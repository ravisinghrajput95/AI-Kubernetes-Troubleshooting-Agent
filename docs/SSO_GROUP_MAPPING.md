# SSO group mapping — worked Okta and Entra ID examples

Backlog item 27. The platform derives a role from the groups an IdP puts in the
token, so there is no second directory to maintain. This is what that looks like
for the two providers an enterprise buyer will actually be running.

## The model, in one paragraph

`AUTH_MODE=oidc` validates the bearer token against your issuer's JWKS, reads a
**subject**, a **groups** list and optionally a **tenant** from claims you name,
and resolves a role. Resolution order is: suspension denies outright; then the
**higher** of the group-derived role and any stored binding (`rbacctl`), because
grants combine; then `RBAC_DEFAULT_ROLE` for a caller matching nothing. Roles
are `viewer` → `operator` → `admin` → `owner`, and each boundary is a capability
rather than a tier — see the *Authorisation* section of `CLAUDE.md`.

```bash
AUTH_MODE=oidc
OIDC_ISSUER=https://example.okta.com/oauth2/default
OIDC_AUDIENCE=api://k8s-agent
OIDC_USERNAME_CLAIM=email          # default
OIDC_GROUPS_CLAIM=groups           # default
OIDC_ROLE_MAPPINGS='platform-sre=admin,oncall=operator,engineering=viewer'
RBAC_DEFAULT_ROLE=viewer
```

`OIDC_JWKS_URL` is optional; without it the platform uses
`<issuer>/.well-known/jwks.json`. A malformed `OIDC_ROLE_MAPPINGS` entry is
**refused at startup**, not skipped — a silently dropped mapping is a customer
whose admins are viewers and whose only symptom is a support ticket.

---

## Okta

Okta emits group **names**, which makes the mapping readable. The claim is not
in the token by default; you have to add it to the authorisation server.

**1. Create the API authorisation server audience.** Security → API → your
authorisation server. Note the issuer URI and set an audience such as
`api://k8s-agent`.

**2. Add a groups claim.** On that authorisation server, Claims → Add Claim:

| Field | Value |
|---|---|
| Name | `groups` |
| Include in token type | **Access Token** |
| Value type | `Groups` |
| Filter | `Matches regex` → `.*`, or `Starts with` → `k8s-` |
| Include in | Any scope |

Use a narrowing filter in preference to `.*` if your directory has many groups —
see *the overage trap* below, which bites Okta far less than Entra but is not
impossible.

**3. Map the groups.**

```bash
OIDC_ISSUER=https://example.okta.com/oauth2/aus1a2b3c4d5e6f7g8
OIDC_AUDIENCE=api://k8s-agent
OIDC_GROUPS_CLAIM=groups
OIDC_ROLE_MAPPINGS='k8s-platform-owners=owner,k8s-sre=admin,k8s-oncall=operator,k8s-eng=viewer'
```

**4. Verify before trusting it.** Decode a real access token and confirm the
claim is present and is a **JSON array**:

```bash
TOKEN=...   # an access token for your audience, not an ID token
python - <<'EOF'
import base64, json, os
payload = os.environ["TOKEN"].split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
print("subject:", claims.get("email") or claims.get("sub"))
print("groups: ", claims.get("groups"))
print("type:   ", type(claims.get("groups")).__name__)
EOF
```

Then confirm the platform agrees:

```bash
curl -sH "Authorization: Bearer $TOKEN" http://localhost:8000/me
# {"subject": "alice@example.com", "role": "admin", "tenant": "default", ...}
```

`/me` is the only route a caller with no role may reach, precisely so this check
works before any grant exists.

---

## Microsoft Entra ID (Azure AD)

Entra has two ways to do this and **the obvious one is the worse one.**

### Option A (recommended): App Roles

App roles are defined on the application, assigned to groups or users, and
arrive in the `roles` claim as the **string values you chose**. They are stable,
readable, and immune to the overage problem below.

**1.** App registrations → your app → App roles → Create. Make four, with
`Value` set to exactly `owner`, `admin`, `operator`, `viewer`, and *Allowed
member types* = **Users/Groups**.

**2.** Enterprise applications → your app → Users and groups → assign each
directory group to the matching app role.

**3.** Point the platform at the `roles` claim instead of `groups`:

```bash
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-guid>/v2.0
OIDC_AUDIENCE=api://k8s-agent
OIDC_USERNAME_CLAIM=preferred_username
OIDC_GROUPS_CLAIM=roles
OIDC_ROLE_MAPPINGS='owner=owner,admin=admin,operator=operator,viewer=viewer'
```

The mapping looks like a tautology and is not: it is what keeps the app-role
vocabulary yours and the platform's role vocabulary the platform's. Rename an
app role later and you change one entry here.

### Option B: the `groups` claim

Works, with two sharp edges.

**Entra emits group object GUIDs, not names**, unless the group is synced from
on-premises AD *and* you opt into sAMAccountName. So the mapping is unreadable:

```bash
OIDC_GROUPS_CLAIM=groups
OIDC_ROLE_MAPPINGS='8f4c1e2a-...-9b3d=admin,2d7a9f01-...-4c6e=operator'
```

Enable it under Token configuration → Add groups claim, choosing **Security
groups** and, on the Access token line, ID = **Group ID** (or sAMAccountName
where available). Keep a comment beside every GUID in your deployment
configuration; nobody will remember which is which.

### The overage trap — read this one

**If a user is a member of more than ~200 groups, Entra omits the `groups`
claim entirely** and substitutes `_claim_names` / `_claim_sources` pointing at
Microsoft Graph. The platform does not call Graph. It reads the configured
claim, finds nothing, and produces **an empty group list** — the token is
perfectly valid and the user simply has no groups.

What that user gets is `RBAC_DEFAULT_ROLE`, and the consequence depends entirely
on your deployment:

| Deployment | An overage user becomes |
|---|---|
| `TENANCY_MODE=single`, default config | **`admin`** — the shipped default |
| `TENANCY_MODE=single`, `RBAC_DEFAULT_ROLE=viewer` | `viewer` |
| `TENANCY_MODE=shared` | nothing; denied everything but `/me` |

The first row is the one to care about: your most heavily-grouped users are
typically your longest-tenured staff, and on a default single-tenant install
they silently land on the deployment default rather than the role you mapped.
This is not a platform bug — `RBAC_DEFAULT_ROLE=admin` is documented as
*today's behaviour preserved exactly* so that upgrading does not lock an
existing install out — but it is a bad interaction with Entra's overage
behaviour.

**Mitigations, in order of preference:** use App Roles (Option A, which has no
overage behaviour); or use a group filter so the claim carries only the handful
of groups that matter; or set `RBAC_DEFAULT_ROLE=viewer` and grant explicitly.

The same reasoning applies to any claim shape that is not a JSON array. The
authenticator accepts a **list** and produces an empty tuple for anything else,
so a provider emitting a space-delimited string yields no groups rather than one
wrong group. That is the safe direction, and it is still silent — verify with
the decode snippet above rather than assuming.

---

## Multi-tenant deployments

In `TENANCY_MODE=shared` the tenant comes from a claim you name, and a token
without it is **rejected** rather than defaulted:

```bash
TENANCY_MODE=shared
OIDC_TENANT_CLAIM=org_id
RBAC_DEFAULT_ROLE=viewer     # refused above viewer in shared mode
```

Placing a user from an unrecognised organisation into the shared default tenant
is the exact failure tenancy exists to prevent, so an absent or unusable claim
fails the login. `shared` also requires `DATABASE_URL` and real authentication;
both are refused at startup otherwise, because there is no in-memory equivalent
of row-level security and every caller being anonymous means every caller is the
same tenant.

## Bootstrapping the first owner

Group mapping cannot grant the first `owner` if your IdP is not yet configured,
and there is deliberately no role-granting HTTP endpoint — one reachable before
any role exists is the hole it would be trying to close. Use the CLI:

```bash
python -m app.rbacctl grant --subject alice@example.com --role owner
python -m app.rbacctl --tenant acme list
python -m app.rbacctl suspend --subject bob@example.com [--restore]
```

A stored binding and a group-derived role **combine** — the caller gets the
higher of the two — so a binding can raise a user above what their groups give
them but can never lower an IdP grant. A binding that could lower one would be
the second directory that group mapping exists to avoid.

## Checklist

- [ ] `/me` returns the expected `role` for a real token from each group
- [ ] A user in no mapped group gets `RBAC_DEFAULT_ROLE` and you are happy with it
- [ ] The groups claim is a JSON array in a decoded **access** token, not an ID token
- [ ] (Entra) A user in >200 groups still resolves correctly, or App Roles are in use
- [ ] The last owner cannot be demoted — try it; it is refused by design
- [ ] `shared` mode: a token with no tenant claim is rejected, not defaulted

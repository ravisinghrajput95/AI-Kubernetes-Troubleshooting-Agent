-- Users and roles inside a tenant.
--
-- M6 put a tenant on every row and made Postgres enforce it. That made a tenant
-- a data boundary and stopped: inside one, every caller who could authenticate
-- could start investigations, mint cluster enrolment tokens and revoke agent
-- certificates. This table is who, within a tenant, may do what.
--
-- Everything here follows 003 rather than inventing a second pattern:
--
--   * `tenant_id` defaults to `current_setting('app.current_tenant')`, so no
--     store method mentions a tenant and inserts are stamped anyway.
--   * ENABLE + FORCE ROW LEVEL SECURITY, because the application connects as
--     the table owner and owners bypass RLS by default. Without FORCE this
--     policy would exist, read correctly, and do nothing.
--   * `WITH CHECK` as well as `USING`. Reading is half the control: one tenant
--     must not be able to write a membership row into another, which would be
--     a cross-tenant privilege grant rather than merely a leak.
--
-- The primary key is `(tenant_id, subject)`, so the same person in two tenants
-- is two independent rows with two independent roles.
CREATE TABLE IF NOT EXISTS tenant_members (
    tenant_id    text NOT NULL DEFAULT current_setting('app.current_tenant', true),
    subject      text NOT NULL,
    email        text NOT NULL DEFAULT '',

    -- NULL means "seen, never granted a role", which is not the same as
    -- 'viewer' and must not be stored as it. Every authenticated request
    -- upserts a row so an admin can find real people in `GET /members`; if
    -- that row carried a role it would carry authority, and would demote a
    -- caller whose role comes from the deployment default on their very next
    -- request. A CHECK constraint permits NULL by design.
    role         text CHECK (role IN ('viewer', 'operator', 'admin', 'owner')),

    -- The one thing that overrides a grant, including one from the identity
    -- provider's groups. An admin has to be able to cut access now rather than
    -- after the customer's directory team gets to it.
    suspended    boolean NOT NULL DEFAULT false,

    granted_by   text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,

    PRIMARY KEY (tenant_id, subject)
);

-- Listing members and counting owners both lead with the tenant, so it leads
-- in the index too. The partial index is what the last-owner check reads.
CREATE INDEX IF NOT EXISTS tenant_members_role_idx
    ON tenant_members (tenant_id, role)
    WHERE role IS NOT NULL AND NOT suspended;

ALTER TABLE tenant_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_members_tenant ON tenant_members;
CREATE POLICY tenant_members_tenant ON tenant_members
    USING (app_tenant_visible(tenant_id))
    WITH CHECK (app_tenant_visible(tenant_id));

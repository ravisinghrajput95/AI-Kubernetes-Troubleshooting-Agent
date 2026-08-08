# MCP server — connecting your own agent

Backlog item 48. The platform exposes its capabilities as [Model Context
Protocol](https://modelcontextprotocol.io) tools, so a customer's own AI agent
can list clusters, start investigations and read diagnoses. This is the
user-facing contract; the reasoning behind the design lives in `CLAUDE.md` and
`docs/ENTERPRISE_ARCHITECTURE.md` §3.7.

## Endpoint

```
POST /mcp
Authorization: Bearer <token>
Content-Type: application/json
```

One endpoint, JSON-RPC 2.0 over HTTP POST. There is no separate MCP port, no
SSE transport and no session handshake beyond `initialize` — the platform's
existing authentication *is* the session.

**Your agent authenticates exactly like any other client.** The same bearer
token, the same `Principal`, the same role. Cluster reads are impersonated as
the calling identity, so an agent cannot see more than the human whose
credential it holds. There is deliberately no separate service-account model
for MCP; a second identity system would be a second place to get authorisation
wrong.

## The supported JSON-RPC subset

**Named rather than implied**, because the server is hand-written rather than
built on the reference SDK — the wire format is small and stable, and the
dependency would bring a transport, a session model and a lifecycle the
platform already has. The cost of that choice is that new spec features do not
arrive for free, so this list is the contract:

| Method | Supported | Notes |
|---|---|---|
| `initialize` | ✅ | Returns `protocolVersion`, server name and capabilities |
| `notifications/initialized` | ✅ | **`204 No Content`** — a notification gets no reply |
| `notifications/cancelled` | ✅ | **`204 No Content`** |
| `ping` | ✅ | Empty result |
| `tools/list` | ✅ | **Filtered by what the caller may actually do** |
| `tools/call` | ✅ | |
| `resources/*` | ❌ | Not implemented — no resource surface is exposed |
| `prompts/*` | ❌ | Not implemented |
| `sampling/*` | ❌ | Not implemented — the platform does not call back into your model |
| `logging/*` | ❌ | Not implemented |
| `completion/*` | ❌ | Not implemented |

Protocol version: **`2025-06-18`**. Server name: `ai-kubernetes-agent`.

Anything not listed returns JSON-RPC `-32601` (method not found).

Batched requests are supported and are answered as a JSON array in request
order. **A notification — single or in a batch — is answered with `204 No
Content` and an empty body**, never with `null`. If your client treats an
empty 204 as a transport error, that is the case to handle.

## Tools

| Tool | Permission required | Costed |
|---|---|---|
| `list_clusters` | `cluster.read` | no |
| `list_investigations` | `investigation.read` | no |
| `get_investigation` | `investigation.read` | no |
| `start_investigation` | `investigation.run` | **yes** |

`tools/list` returns only the tools the caller is permitted to use. A viewer
sees three; an operator sees four. That is deliberate: listing a tool that every
call would refuse teaches an agent to keep trying it, and an agent that keeps
trying is an agent that keeps burning your rate limit.

### `start_investigation`

```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {
    "name": "start_investigation",
    "arguments": {"cluster": "prod-eu-1", "namespace": "payments"}
  }
}
```

Returns the investigation id **immediately** and does not wait. Poll
`get_investigation` for status and, once finished, the diagnosis. `namespace` is
optional; omitting it investigates the whole cluster.

This is the one costed tool. It reads a production cluster under your
impersonated identity and spends a model call, which is why it needs
`investigation.run` and why it shares the HTTP API's rate-limit buckets rather
than having its own — a second entry point with its own budget would double the
quota an operator configured.

### `get_investigation`

```json
{"name": "get_investigation", "arguments": {"investigation_id": "..."}}
```

Status while running; status plus the diagnosis once finished. Ownership is
enforced: an id you do not own returns not-found rather than forbidden, so the
endpoint cannot be used as an existence oracle for other people's
investigations.

## Errors

JSON-RPC errors, **not** HTTP status codes. A refused tool call is still a
successful *request*; conflating the two would make a permission denial
indistinguishable from a malformed envelope to a client that only inspects the
transport. Expect HTTP 200 with an `error` member.

| Code | Meaning |
|---|---|
| `-32700` | Parse error |
| `-32600` | Invalid request (not JSON-RPC 2.0) |
| `-32601` | Unknown method, or unknown tool |
| `-32602` | Invalid params for the named tool |
| `-32603` | Internal error |
| `-32000` | **Not permitted** — you are authenticated but lack the permission |

`-32000` is deliberately distinct from `-32601` so a client can tell "this
server has no such tool" from "you may not use it", and so the two cannot be
confused by a caller retrying with different credentials.

HTTP-level failures still behave normally: a missing or invalid bearer token is
`401` before any JSON-RPC parsing happens, and exceeding the rate limit is
`429` with `Retry-After`.

## What is deliberately not exposed

**Nothing that mutates the fleet.** No agent enrolment, no revocation, no member
or role management. Those require `admin` and are the operations most likely to
be destructive; handing them to an autonomous agent is a decision a customer
should make explicitly, at a console, rather than inherit from a tool list.

If you need those, use the HTTP API or the CLIs (`agentctl`, `rbacctl`).

## Worked example

```bash
TOKEN=...   # the same bearer token your users hold

mcp() {
  curl -sS -X POST http://localhost:8000/mcp \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -d "$1"
}

# 1. Handshake
mcp '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# 2. What may this identity do?
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# 3. Start an investigation
mcp '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
       "name":"start_investigation",
       "arguments":{"cluster":"prod-eu-1","namespace":"payments"}}}'

# 4. Poll it
mcp '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
       "name":"get_investigation",
       "arguments":{"investigation_id":"<id from step 3>"}}}'
```

## Configuring a client

Most MCP clients expect a command or a URL plus headers. For a streamable-HTTP
client:

```json
{
  "mcpServers": {
    "k8s-agent": {
      "url": "https://k8s-agent.example.com/mcp",
      "headers": {"Authorization": "Bearer ${K8S_AGENT_TOKEN}"}
    }
  }
}
```

A client expecting stdio needs a bridge; the platform speaks HTTP only.

## Operational notes

- **`/mcp` is one of exactly two routes marked `AUTHENTICATED`** in the
  authorisation table, and it means something different from the other (`/me`):
  not "nothing to check" but "checked deeper" — per tool, inside the handler,
  against the same permission set the HTTP routes use.
- **A tool with no permission entry cannot be called.** New tools fail closed by
  construction rather than by review.
- Rate limiting is shared with the HTTP API. `RATE_LIMIT_PER_MINUTE` (default
  60) and `RATE_LIMIT_TENANT_PER_MINUTE` apply to `start_investigation` and to
  nothing else here, because reads of already-collected data cost neither a
  cluster read nor a model call.
- With `AUTH_MODE=disabled` every caller is anonymous and resolves to `owner`,
  so **do not expose `/mcp` on a deployment running without authentication.**

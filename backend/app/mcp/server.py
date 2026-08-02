"""JSON-RPC dispatch for the MCP surface.

A hand-written subset — `initialize`, `tools/list`, `tools/call` — rather than
the reference SDK, for the reason that kept axios out of the console: the wire
format is a small, stable, well-specified thing, and the dependency would bring
a transport layer, a session model and a lifecycle this platform already has
its own versions of. What it costs is that new spec features do not arrive for
free, which is the honest trade and is why the supported subset is named here
rather than implied.

**Authorisation is not optional and not per-tool code.** Every call goes
through the same `Permission` a route would need, taken from the tool registry,
and a tool with no entry there cannot be reached at all. The identity is the
same `Principal` the HTTP API authenticates — an MCP client holds a bearer
token like any other client — so cluster reads are impersonated exactly as they
would be from the console. There is no second identity model here, which is the
whole reason this is safe to expose.

Errors are JSON-RPC errors, not HTTP status codes. A tool call that is refused
still succeeded as a *request*; conflating the two would make a permission
denial indistinguishable from a malformed envelope to a client that only
inspects the transport.
"""

from typing import Any

from loguru import logger

from app.auth.models import Principal
from app.authz.dependencies import grant_for
from app.authz.routes import is_costed
from app.mcp.tools import PROTOCOL_VERSION, SERVER_NAME, TOOLS, get_tool

# JSON-RPC 2.0 reserved codes, plus the one application code this uses.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Application-defined range. Distinct from METHOD_NOT_FOUND so a client can
# tell "this server has no such tool" from "you may not use it" — and so the
# two cannot be confused by a caller retrying with different credentials.
NOT_PERMITTED = -32000


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


async def handle(message: dict[str, Any], principal: Principal) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. `None` means it was a notification.

    A notification has no id and expects no reply; returning one anyway is a
    protocol violation that some clients treat as a fatal error.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 message")

    method = message.get("method")
    request_id = message.get("id")
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _error(request_id, INVALID_REQUEST, "Missing method")

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        )

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None

    if method == "ping":
        return None if is_notification else _result(request_id, {})

    if method == "tools/list":
        # Filtered by what this caller may actually do. Listing a tool that
        # every call would refuse teaches an agent to keep trying it, and
        # burns a turn each time it does.
        grant = grant_for(principal)
        return _result(
            request_id,
            {
                "tools": [
                    tool.describe() for tool in TOOLS.values() if grant.permits(tool.permission)
                ]
            },
        )

    if method == "tools/call":
        return await _call(message, request_id, principal)

    return (
        None
        if is_notification
        else _error(request_id, METHOD_NOT_FOUND, f"Unknown method {method}")
    )


async def _call(message: dict[str, Any], request_id: Any, principal: Principal) -> dict[str, Any]:
    params = message.get("params")
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "params must be an object")

    name = params.get("name")
    tool = get_tool(name) if isinstance(name, str) else None
    if tool is None:
        # Absent and unknown answer alike. A tool missing from the registry is
        # not callable, which is the default this surface inherits from
        # `ROUTE_PERMISSIONS` rather than reimplements.
        return _error(request_id, METHOD_NOT_FOUND, f"Unknown tool {name!r}")

    grant = grant_for(principal)
    if not grant.permits(tool.permission):
        held = f"the {grant.role} role" if grant.role else "no role in this tenant"
        return _error(
            request_id,
            NOT_PERMITTED,
            f"{tool.name} requires '{tool.permission}'; you hold {held}.",
        )

    if is_costed(tool.permission):
        # The same limiter and the same buckets as the HTTP surface. A second
        # entry point with its own budget would double the quota an operator
        # thought they had configured.
        from app.core.config import settings
        from app.ratelimit import evaluate, get_rate_limiter

        decision = evaluate(
            get_rate_limiter(),
            subject=principal.subject,
            tenant=principal.tenant,
            subject_limit=settings.rate_limit_per_minute,
            tenant_limit=settings.rate_limit_tenant_per_minute,
        )
        if not decision.allowed:
            from app.observability import metrics

            metrics.rate_limited(decision.scope)
            return _error(request_id, NOT_PERMITTED, decision.detail)

    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return _error(request_id, INVALID_PARAMS, "arguments must be an object")

    try:
        payload = await tool.handler(principal, **(arguments or {}))
    except TypeError as exc:
        # An argument the tool does not take. A client error, not ours.
        return _error(request_id, INVALID_PARAMS, str(exc))
    except Exception as exc:
        # Deliberately not echoed to the caller. A tool failure can carry
        # cluster text, and this surface exists to be consumed by a model —
        # the same reasoning that keeps raw evidence out of `app/notify`.
        logger.opt(exception=exc).error("MCP tool {tool} failed", tool=tool.name)
        return _error(request_id, INTERNAL_ERROR, f"{tool.name} failed")

    return _result(request_id, {"content": [{"type": "text", "text": _as_text(payload)}]})


def _as_text(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2, default=str)

"""The MCP endpoint.

Authenticated exactly like the rest of the API — an MCP client holds a bearer
token, so `require_principal` applies and cluster reads are impersonated as the
caller. Authorisation is *not* done here: it is per tool, in
`app/mcp/server.py`, because one endpoint serving many capabilities cannot be
gated by a single route permission.

That is why `AUTHENTICATED` appears against this route in `ROUTE_PERMISSIONS`
rather than a real permission. It is the one place that marker means "the check
happens deeper" instead of "there is nothing to check", and `tests/test_mcp.py`
asserts every tool carries one so the deeper check cannot be skipped.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.auth.dependencies import require_principal
from app.auth.models import Principal
from app.authz.dependencies import require_permission
from app.mcp import handle

router = APIRouter(tags=["mcp"], dependencies=[Depends(require_permission)])


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Any:
    """One JSON-RPC message, or a batch of them.

    A batch containing only notifications produces no response body, which the
    spec requires and which a client will otherwise treat as a malformed reply.
    """
    try:
        message = await request.json()
    except Exception:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        }

    if isinstance(message, list):
        replies = [reply for reply in [await handle(one, principal) for one in message] if reply]
        return replies or None

    return await handle(message, principal)

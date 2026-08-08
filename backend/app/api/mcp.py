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

from fastapi import APIRouter, Depends, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

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

    **A notification produces `204 No Content`, not a body.** Returning `None`
    from this handler was not the same thing: FastAPI serialises it as the JSON
    literal `null`, so a client that had just sent `notifications/initialized`
    received four bytes where the spec says it should receive nothing, and a
    strict client parsing that as a response object finds `null` is not one.

    The tests could not see the difference — `response.json() is None` is true
    both for an empty body and for a body that literally is `null` — which is
    why the docstring claimed one behaviour while the wire did the other for as
    long as it did.
    """
    try:
        message = await request.json()
    except Exception:
        return _json(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        )

    if isinstance(message, list):
        replies = [reply for reply in [await handle(one, principal) for one in message] if reply]
        return _json(replies) if replies else Response(status_code=204)

    reply = await handle(message, principal)
    return _json(reply) if reply is not None else Response(status_code=204)


def _json(payload: Any) -> Response:
    """Serialise explicitly, so the notification path can return no body at all.

    A plain `return payload` would let FastAPI's default response model turn
    `None` into `null`, which is the bug this shape exists to avoid.
    """
    return JSONResponse(content=jsonable_encoder(payload))

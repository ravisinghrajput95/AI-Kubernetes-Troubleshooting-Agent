"""MCP: the platform's capabilities as tools a customer's own agent can call.

`tools.py` carries the decision that matters — a tool with no permission in the
registry cannot be called, which is `ROUTE_PERMISSIONS`' default-deny applied to
a surface the router-level dependency cannot reach.

`server.py` carries the JSON-RPC subset and the reason it is hand-written.
"""

from app.mcp.server import handle
from app.mcp.tools import PROTOCOL_VERSION, SERVER_NAME, TOOLS, Tool, get_tool

__all__ = ["PROTOCOL_VERSION", "SERVER_NAME", "TOOLS", "Tool", "get_tool", "handle"]

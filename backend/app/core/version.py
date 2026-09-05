"""The platform's version, in one place.

There were two hardcoded copies and they had already drifted: `app/main.py`
served `0.2.0` on `/openapi.json` while `app/mcp/server.py` told every MCP
client `1.0.0` — a version this project has never released. An agent gating on
`serverInfo.version`, or a support engineer reading it out of a log, got an
answer that was simply wrong, and nothing objected because the MCP test asserted
only that the name was truthy.

A bare module with no imports, so anything can read it without a cycle —
`app/core/config` pulls in pydantic, and `app/__init__` must keep its
`GRPC_ENABLE_FORK_SUPPORT` setdefault first (see F22), so neither is a safe home.

Bumping this is part of cutting a release: `tests/test_documentation.py` holds
it against a matching section in `CHANGELOG.md`, and `tests/test_mcp.py` holds
the MCP handshake against it, so a bump without release notes fails and a
release without a bump fails.
"""

VERSION = "0.2.1"

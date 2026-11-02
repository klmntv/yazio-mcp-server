"""Assembles the MCP server and its ASGI app."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from .local_auth import SessionManager
from .middleware import BasicAuthMiddleware
from .tools import register_all

INSTRUCTIONS = """\
Tools for a YAZIO food diary: reading what was eaten and how it compares to the
day's goals, searching YAZIO's food database, logging food, water, weight and
activity, and building recipes.

Start with get_daily_summary for how a day is going, get_diary for what was
actually eaten, and get_nutrition_range for trends across days. To log food,
search_products first — tracking needs the product_id that search returns.

Use list_saved_meals/get_saved_meal for existing YAZIO food combinations.
Food writes require request_id: reuse it to reconcile a lost response, and use
a new one only for a new user action, even when the foods are identical.
Report success only for status=committed and verified=true. Partial/uncertain
results must be described honestly; never resend them with a new request ID.

Energy is in kilocalories, macros in grams, and vitamins and minerals in
milligrams. Every nutrient block states its own units. Dates are YYYY-MM-DD and
default to today when omitted.
"""

# Hosts the transport accepts without being told to. FastMCP applies these on
# its own when bound to loopback; naming them here keeps the set the same once
# extra hosts are added, instead of silently replacing it.
_LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def build_server(
    allowed_hosts: Sequence[str] | None = None,
    *,
    session_manager: SessionManager | None = None,
    state_dir: Path | None = None,
    read_only: bool = False,
) -> FastMCP:
    """Create the FastMCP server with every tool registered.

    Runs stateless because each request authenticates on its own: there is no
    per-session state worth keeping between calls, and stateless mode lets any
    number of clients share the endpoint without session bookkeeping.

    Args:
        allowed_hosts: Extra Host header values to accept, such as
            "yazio.example.com" or "yazio.example.com:*". The transport rejects
            unlisted hosts as DNS-rebinding attempts, which is what a request
            arriving through a reverse proxy under a real hostname looks like
            unless that hostname is named here.
    """
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*_LOOPBACK_HOSTS, *allowed_hosts],
            allowed_origins=[
                *_LOOPBACK_ORIGINS,
                *(f"https://{host}" for host in allowed_hosts),
                *(f"http://{host}" for host in allowed_hosts),
            ],
        )

    @asynccontextmanager
    async def lifespan(_server):
        yield {"session_manager": session_manager, "state_dir": state_dir}

    mcp = FastMCP(
        name="yazio",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        transport_security=security,
        lifespan=lifespan,
    )
    register_all(mcp)
    if read_only:
        # Registering only reads makes the restriction independent of model instructions.
        for tool in list(mcp._tool_manager.list_tools()):
            if not tool.name.startswith(("get_", "list_", "search_", "browse_")):
                mcp.remove_tool(tool.name)
    return mcp


def build_app(allowed_hosts: Sequence[str] | None = None) -> Starlette:
    """Create the ASGI app, gated behind Basic auth."""
    mcp = build_server(allowed_hosts)
    app = mcp.streamable_http_app()
    app.add_middleware(BasicAuthMiddleware)
    return app

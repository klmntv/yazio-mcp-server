"""The HTTP surface: tool registration and the Basic-auth gate."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager

import httpx
import pytest

from yazio_mcp.server import build_app, build_server

# Every tool the server is expected to expose. Listed explicitly so that
# accidentally dropping a registration fails the suite rather than going unnoticed.
EXPECTED_TOOLS = {
    "list_saved_meals",
    "get_saved_meal",
    "track_saved_meal",
    "get_operation",
    "browse_recipes",
    "create_recipe",
    "delete_recipe",
    "delete_recipe_photo",
    "get_activity_summary",
    "get_daily_summary",
    "get_diary",
    "get_favorite_recipes",
    "get_goals",
    "get_last_weight",
    "get_nutrition_range",
    "get_product",
    "get_profile",
    "get_recipe",
    "get_suggested_products",
    "get_water",
    "list_my_recipes",
    "log_exercise",
    "log_water",
    "log_weight",
    "search_products",
    "set_recipe_photo",
    "track_product",
    "track_recipe",
    "untrack_item",
    "update_recipe",
}


@pytest.mark.asyncio
async def test_every_tool_is_registered():
    tools = await build_server().list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_every_tool_is_described():
    """Descriptions are how a model picks a tool, so none may be missing."""
    for tool in await build_server().list_tools():
        assert tool.description, f"{tool.name} has no description"


@pytest.mark.asyncio
async def test_read_only_mode_does_not_expose_mutations():
    names = {tool.name for tool in await build_server(read_only=True).list_tools()}
    assert {"list_saved_meals", "get_saved_meal", "get_diary"} <= names
    assert not {"track_product", "track_recipe", "track_saved_meal", "untrack_item"} & names


@pytest.mark.asyncio
async def test_the_mcp_context_is_not_exposed_as_an_argument():
    """`ctx` is injected by FastMCP; a caller must never be asked to supply it."""
    for tool in await build_server().list_tools():
        assert "ctx" not in tool.inputSchema.get("properties", {})


@pytest.mark.asyncio
async def test_dates_are_optional_everywhere():
    """Every date argument defaults to today, so none may be required."""
    for tool in await build_server().list_tools():
        assert "date" not in tool.inputSchema.get("required", [])


@asynccontextmanager
async def running_app():
    """Serve the app over an in-process transport, with its lifespan running.

    The lifespan matters: the streamable HTTP transport starts its task group
    there and refuses every request until it has. It is a plain context manager
    rather than a fixture because the anyio scope it opens has to be closed by
    the same task that opened it, which a yielding async fixture does not
    guarantee.
    """
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        # Loopback in the URL, because the transport's DNS-rebinding guard
        # rejects Host headers it was not told about.
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


@pytest.mark.asyncio
async def test_a_request_without_credentials_is_refused():
    async with running_app() as client:
        response = await client.post("/mcp", json={})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_the_refusal_carries_a_basic_challenge():
    async with running_app() as client:
        response = await client.post("/mcp", json={})

    assert response.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.asyncio
async def test_a_bearer_token_is_refused():
    """Only Basic is accepted; the credentials are the YAZIO login itself."""
    async with running_app() as client:
        response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer abc"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_credentials_are_refused():
    async with running_app() as client:
        response = await client.post("/mcp", json={}, headers={"Authorization": "Basic ###"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_well_formed_credentials_pass_the_gate():
    """The gate checks shape only — whether YAZIO accepts them comes later."""
    encoded = base64.b64encode(b"me@example.com:hunter2").decode()

    async with running_app() as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Basic {encoded}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code != 401


@pytest.mark.asyncio
async def test_the_x_auth_pair_passes_the_gate():
    """Clients that cannot build a Basic credential send the two plain headers."""
    async with running_app() as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "X-Auth-Username": "me@example.com",
                "X-Auth-Password": "hunter2",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code != 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Auth-Username": "me@example.com"},
        {"X-Auth-Password": "hunter2"},
        {"X-Auth-Username": "me@example.com", "X-Auth-Password": ""},
        {"X-Auth-Username": "", "X-Auth-Password": ""},
    ],
)
async def test_an_unusable_x_auth_pair_is_refused_as_a_bad_request(headers):
    async with running_app() as client:
        response = await client.post("/mcp", json={}, headers=headers)

    assert response.status_code == 400
    # No challenge: the client did try, and resending the same pair cannot help.
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_an_extra_allowed_host_is_accepted():
    """A server behind a proxy must accept the proxy's Host header."""
    app = build_app(["yazio.example.com"])
    encoded = base64.b64encode(b"me:pw").decode()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://yazio.example.com"
        ) as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )

    assert response.status_code != 421

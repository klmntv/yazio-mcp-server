"""Builds an authenticated YAZIO client for the request a tool is serving.

Credentials are per request, not per process, so a tool cannot hold onto a
long-lived client. Instead it opens one for the duration of a call:

    async with yazio_client(ctx) as client:
        response = await some_endpoint.asyncio_detailed(client=client)
        data = expect_ok(ctx, response, "load the diary")

The credentials are read straight off the Starlette request that the streamable
HTTP transport attaches to the MCP request context. That is deliberate: reading
them from a context variable set in middleware would depend on how the transport
happens to spawn its tasks, whereas the request object is passed through
explicitly and is stable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk import AuthenticatedClient
from yazio_sdk.types import Response

from .auth import AuthError, Credentials, TokenCache, resolve_credentials
from .config import BASE_URL, DEFAULT_HEADERS, HTTP_TIMEOUT
from .state import state_directory

T = TypeVar("T")

# One cache for the whole process. Entries are keyed by a hash of the
# credentials, so concurrent users never see each other's tokens.
_TOKENS = TokenCache()
EXPECTED_ACCOUNT: ContextVar[str | None] = ContextVar("yazio_expected_account", default=None)
ACTIVE_TOKEN: ContextVar[str | None] = ContextVar("yazio_active_token", default=None)


def local_context(ctx: Context) -> dict[str, Any]:
    try:
        value = ctx.request_context.lifespan_context
        return value if isinstance(value, dict) else {}
    except (AttributeError, LookupError, ValueError):
        return {}


def operation_scope(ctx: Context) -> str:
    manager = local_context(ctx).get("session_manager")
    if manager is not None:
        session = manager.load()
        if session is None:
            raise ToolError("Sign in locally before using YAZIO tools")
        return session.scope
    return credentials_from_context(ctx).cache_key


def data_directory(ctx: Context):
    return local_context(ctx).get("state_dir") or state_directory()


def credentials_from_context(ctx: Context) -> Credentials:
    """Extract the caller's YAZIO login from the in-flight HTTP request."""
    request = ctx.request_context.request
    headers = getattr(request, "headers", None)
    if headers is None:
        raise AuthError(
            "no HTTP request on this MCP context; the server must be reached "
            "over the streamable HTTP transport with credential headers"
        )

    return resolve_credentials(headers)


@asynccontextmanager
async def yazio_client(ctx: Context) -> AsyncIterator[AuthenticatedClient]:
    """Yield a YAZIO client authenticated as the caller."""
    try:
        manager = local_context(ctx).get("session_manager")
        if manager is not None:
            token = await manager.token(expected_scope=EXPECTED_ACCOUNT.get())
        else:
            credentials = credentials_from_context(ctx)
            token = await _TOKENS.token_for(credentials)
    except AuthError as exc:
        raise ToolError(f"Not authenticated with YAZIO: {exc}") from exc

    client = AuthenticatedClient(
        base_url=BASE_URL,
        token=token,
        timeout=HTTP_TIMEOUT,
        headers=DEFAULT_HEADERS,
        raise_on_unexpected_status=False,
    )

    active = ACTIVE_TOKEN.set(token)
    try:
        async with client as client:
            yield client
    finally:
        ACTIVE_TOKEN.reset(active)


def expect_ok(ctx: Context, response: Response[T], what: str) -> T:
    """Return a parsed response body, or raise a readable tool error.

    A 401 here means the cached token was rejected even though we thought it was
    still valid — a password change, or a session revoked on another device. The
    token is evicted so the next call re-authenticates rather than replaying a
    token we already know is dead.
    """
    if response.status_code == 401:
        _invalidate(ctx)
        raise ToolError(
            f"YAZIO rejected the stored session while trying to {what}. "
            "Check the username and password the client is sending."
        )

    if not 200 <= response.status_code < 300:
        raise ToolError(
            f"YAZIO returned {response.status_code} while trying to {what}: "
            f"{_snippet(response.content)}"
        )

    if response.parsed is None:
        raise ToolError(
            f"YAZIO returned a response we could not parse while trying to {what}: "
            f"{_snippet(response.content)}"
        )

    return response.parsed


def expect_written(ctx: Context, response: Response[Any], what: str) -> None:
    """Assert that a write succeeded.

    Separate from `expect_ok` because YAZIO's write endpoints answer with an
    empty body, so there is nothing to parse and `parsed is None` is the normal,
    successful case rather than a failure.
    """
    if response.status_code == 401:
        _invalidate(ctx)
        raise ToolError(
            f"YAZIO rejected the stored session while trying to {what}. "
            "Check the username and password the client is sending."
        )

    if not 200 <= response.status_code < 300:
        raise ToolError(
            f"YAZIO returned {response.status_code} while trying to {what}: "
            f"{_snippet(response.content)}"
        )


def _invalidate(ctx: Context) -> None:
    manager = local_context(ctx).get("session_manager")
    if manager is not None:
        manager.invalidate(ACTIVE_TOKEN.get())
        return
    try:
        _TOKENS.invalidate(credentials_from_context(ctx))
    except AuthError:
        # Nothing cached to evict if we cannot even read the credentials.
        pass


def _snippet(content: bytes, limit: int = 300) -> str:
    # Upstream errors can echo submitted credentials or health data.
    if b'"version_blocked"' in content:
        return "version_blocked: update the configured YAZIO client version"
    return "(upstream response redacted)" if content else "(empty response body)"

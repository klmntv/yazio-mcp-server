"""Persistent account-bound sessions, refresh rotation, and failure isolation."""

import asyncio
import json
import time

import httpx
import pytest
import respx

from yazio_mcp.auth import AuthError
from yazio_mcp.config import BASE_URL
from yazio_mcp.local_auth import WEB_TOKEN_URL, LocalSession, SessionManager
from yazio_mcp.state import save_private_json
from yazio_mcp.web_auth import public_address

PROFILE = {"uuid": "11111111-1111-4111-8111-111111111111"}
TOKEN_URL = f"{BASE_URL}/v22/oauth/token"
USER_URL = f"{BASE_URL}/v22/user"


def stored(manager, **overrides):
    save_private_json(
        manager.path,
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": time.time() - 10,
            "account_id": PROFILE["uuid"],
            **overrides,
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_password_is_exchanged_but_never_saved(tmp_path):
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            },
        )
    )
    respx.get(USER_URL).mock(return_value=httpx.Response(200, json=PROFILE))
    manager = SessionManager(tmp_path / "private")
    await manager.login("me@example.test", "never-save-this-password")
    data = manager.path.read_text()
    assert "never-save-this-password" not in data
    assert "me@example.test" not in data
    assert json.loads(token.calls[0].request.content)["password"] == "never-save-this-password"
    assert manager.path.stat().st_mode & 0o777 == 0o600
    assert manager.directory.stat().st_mode & 0o777 == 0o700
    assert manager.load().account_id == PROFILE["uuid"]
    assert "access" not in json.dumps(manager.status())


@pytest.mark.asyncio
@respx.mock
async def test_failed_login_does_not_replace_an_existing_session_or_echo_secrets(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager)
    before = manager.path.read_bytes()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "password": "sensitive-password",
                "access_token": "sensitive-token",
            },
        )
    )
    with pytest.raises(AuthError) as error:
        await manager.login("me@example.test", "sensitive-password")
    assert "sensitive" not in str(error.value)
    assert manager.path.read_bytes() == before


@pytest.mark.asyncio
@respx.mock
async def test_invalid_import_does_not_replace_a_working_session(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager)
    before = manager.path.read_bytes()
    respx.get(USER_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError, match="did not accept"):
        await manager.import_session({"access_token": "invalid", "expires_in": 3600})
    assert manager.path.read_bytes() == before


@pytest.mark.asyncio
@respx.mock
async def test_rotated_refresh_is_saved_and_omitted_refresh_is_preserved(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager)
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
            ),
            httpx.Response(200, json={"access_token": "newer", "expires_in": 3600}),
        ]
    )
    respx.get(USER_URL).mock(return_value=httpx.Response(200, json=PROFILE))
    assert await manager.token() == "new"
    assert manager.load().refresh_token == "rotated"
    manager.invalidate()
    assert await manager.token() == "newer"
    assert manager.load().refresh_token == "rotated"
    assert json.loads(route.calls[1].request.content)["refresh_token"] == "rotated"


@pytest.mark.asyncio
@respx.mock
async def test_two_managers_share_one_refresh_and_verify_the_same_account(tmp_path):
    first, second = SessionManager(tmp_path), SessionManager(tmp_path)
    stored(first)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "rotated",
                "expires_in": 3600,
            },
        )
    )
    respx.get(USER_URL).mock(return_value=httpx.Response(200, json=PROFILE))
    assert await asyncio.gather(first.token(), second.token()) == ["fresh", "fresh"]
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_refresh_cannot_silently_switch_accounts(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager)
    before = manager.path.read_bytes()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "foreign",
                "expires_in": 3600,
            },
        )
    )
    respx.get(USER_URL).mock(return_value=httpx.Response(200, json={"uuid": "another-account"}))
    with pytest.raises(AuthError, match="different"):
        await manager.token()
    assert manager.path.read_bytes() == before


@pytest.mark.asyncio
@respx.mock
async def test_web_sessions_renew_at_the_official_bff_without_mobile_client_secret(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager, issuer="web", client_id="public-website-client")
    route = respx.post(WEB_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "web-fresh",
                "refresh_token": "web-rotated",
                "expires_in": 3600,
            },
        )
    )
    respx.get(USER_URL).mock(return_value=httpx.Response(200, json=PROFILE))
    assert await manager.token() == "web-fresh"
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "client_id": "public-website-client",
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
    }
    assert route.calls[0].request.headers["x-client-type"] == "web"
    assert manager.load().issuer == "web"


@pytest.mark.asyncio
async def test_expired_access_without_refresh_requires_login(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager, refresh_token=None)
    with pytest.raises(AuthError, match="cannot renew"):
        await manager.token()


@pytest.mark.asyncio
async def test_an_in_flight_operation_cannot_switch_to_a_new_login(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager, expires_at=time.time() + 3600)
    original_scope = manager.load().scope
    stored(manager, account_id="another-account", expires_at=time.time() + 3600)
    with pytest.raises(AuthError, match="active account changed"):
        await manager.token(expected_scope=original_scope)


@pytest.mark.asyncio
@respx.mock
async def test_a_late_rejection_of_an_old_token_does_not_invalidate_a_new_one(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager, access_token="new-token", expires_at=time.time() + 3600)
    manager.invalidate("old-rejected-token")
    assert await manager.token() == "new-token"


def test_symlinked_or_world_readable_sessions_are_refused(tmp_path):
    manager = SessionManager(tmp_path)
    stored(manager)
    manager.path.chmod(0o644)
    with pytest.raises(AuthError, match="permissions"):
        manager.load()
    manager.path.unlink()
    target = tmp_path / "other.json"
    save_private_json(target, {"access_token": "secret"})
    manager.path.symlink_to(target)
    with pytest.raises(AuthError):
        manager.load()


@pytest.mark.parametrize("expiry", [float("nan"), float("inf"), -1, True, "tomorrow"])
def test_invalid_expiry_is_refused(expiry):
    with pytest.raises(AuthError):
        LocalSession.parse({"access_token": "secret", "expires_at": expiry})


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "10.0.0.1", "www.example.com"])
def test_web_dns_override_never_accepts_private_hosts(address):
    with pytest.raises(ValueError):
        public_address(address)

"""Local login: persist tokens, never passwords, and preserve refresh rotation."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .auth import AuthError
from .config import BASE_URL, CLIENT_ID, CLIENT_SECRET, DEFAULT_HEADERS, HTTP_TIMEOUT
from .state import private_json, save_private_json, state_lock

WEB_TOKEN_URL = "https://www.yazio.com/api/auth/token"
WEB_CLIENT_ID = "7_9236kdzucvnd624cklgz787kcvhjfch6sbdejgmdv8387fj7v"


def _secret(value: Any, label: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 32_768:
        raise AuthError(f"The session has no valid {label}")
    return value


@dataclass(repr=False)
class LocalSession:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float
    account_id: str = field(default="", repr=False)
    issuer: str = "mobile"
    web_address: str | None = None
    client_id: str = field(default=CLIENT_ID, repr=False)
    client_secret: str | None = field(default=CLIENT_SECRET, repr=False)

    @classmethod
    def parse(cls, data: dict[str, Any], previous: LocalSession | None = None):
        access = _secret(data.get("access_token"), "access token")
        refresh = _secret(data.get("refresh_token"), "refresh token", required=False)
        if refresh is None and previous is not None:
            refresh = previous.refresh_token
        expiry = data.get("expires_at")
        if expiry is None:
            duration = data.get("expires_in", 3600)
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise AuthError("The session has no valid expiry")
            if not math.isfinite(duration) or duration <= 0:
                raise AuthError("The session has no valid expiry")
            expiry = time.time() + duration
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            raise AuthError("The session has no valid expiry")
        if expiry > 1_000_000_000_000:
            expiry /= 1000  # Accept the Unix-millisecond exports used by JS clients.
        if not math.isfinite(expiry) or expiry <= 0:
            raise AuthError("The session has no valid expiry")
        issuer = data.get("issuer", previous.issuer if previous else "mobile")
        if issuer not in {"mobile", "web"}:
            raise AuthError("Unknown session issuer")
        web_address = data.get("web_address", previous.web_address if previous else None)
        if web_address is not None:
            from .web_auth import public_address

            try:
                web_address = public_address(web_address)
            except (TypeError, ValueError):
                raise AuthError("The optional YAZIO web address is invalid") from None
        default_client = WEB_CLIENT_ID if issuer == "web" else CLIENT_ID
        client_id = data.get("client_id", previous.client_id if previous else default_client)
        client_secret = data.get(
            "client_secret", previous.client_secret if previous else CLIENT_SECRET
        )
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=float(expiry),
            account_id=previous.account_id if previous else str(data.get("account_id", "")),
            issuer=issuer,
            web_address=web_address,
            client_id=_secret(client_id, "application client ID"),
            client_secret=(
                _secret(client_secret, "application client secret") if issuer == "mobile" else None
            ),
        )

    @property
    def scope(self) -> str:
        if not self.account_id:
            raise AuthError("The session has not been verified against a YAZIO account")
        return hashlib.sha256(self.account_id.encode()).hexdigest()


class SessionManager:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "session.json"
        self.rejected_access_token: str | None = None
        self.last_access_token: str | None = None

    def load(self) -> LocalSession | None:
        try:
            data = private_json(self.path)
            return LocalSession.parse(data) if data is not None else None
        except (OSError, ValueError):
            raise AuthError(
                "Unable to read private session state; check its format and permissions"
            ) from None

    async def exchange(self, values: dict[str, str], previous: LocalSession | None = None):
        payload = {
            "client_id": previous.client_id if previous else CLIENT_ID,
            **values,
        }
        issuer = previous.issuer if previous else "mobile"
        url = WEB_TOKEN_URL if issuer == "web" else f"{BASE_URL}/v22/oauth/token"
        headers = dict(DEFAULT_HEADERS)
        if issuer == "web":
            headers["x-client-type"] = "web"
        else:
            payload["client_secret"] = previous.client_secret if previous else CLIENT_SECRET
        try:
            if issuer == "web" and previous.web_address:
                from .web_auth import post_token

                status, data = await asyncio.to_thread(post_token, previous.web_address, payload)
            else:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
                    response = await client.post(url, json=payload)
                    status = response.status_code
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
        except (httpx.HTTPError, OSError, ValueError):
            raise AuthError(
                "Cannot reach YAZIO to authenticate; no login state was changed"
            ) from None
        if status != 200:
            raise AuthError(f"YAZIO rejected the login or session renewal (HTTP {status})")
        try:
            if not isinstance(data, dict):
                raise ValueError
            return LocalSession.parse(data, previous)
        except (ValueError, TypeError):
            raise AuthError("YAZIO returned an invalid login response") from None

    async def verify(self, session: LocalSession) -> None:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=DEFAULT_HEADERS) as client:
                response = await client.get(
                    f"{BASE_URL}/v22/user",
                    headers={"Authorization": f"Bearer {session.access_token}"},
                )
            if response.status_code != 200:
                raise AuthError(f"YAZIO did not accept this session (HTTP {response.status_code})")
            profile = response.json()
            account = profile.get("uuid") or profile.get("id")
            if not isinstance(account, str) or not account:
                raise AuthError("YAZIO returned a profile without an account identifier")
            if session.account_id and session.account_id != account:
                raise AuthError("This session belongs to a different YAZIO account")
            session.account_id = account
        except (httpx.HTTPError, ValueError, AttributeError):
            raise AuthError("Unable to verify the YAZIO account; no session was saved") from None

    async def login(self, username: str, password: str) -> None:
        async with state_lock(self.directory, "session.lock"):
            session = await self.exchange(
                {
                    "grant_type": "password",
                    "username": username,
                    "password": password,
                }
            )
            await self.verify(session)
            save_private_json(self.path, asdict(session))
            self.rejected_access_token = None

    async def import_session(self, values: dict[str, Any]) -> None:
        async with state_lock(self.directory, "session.lock"):
            session = LocalSession.parse(values)
            await self.verify(session)
            save_private_json(self.path, asdict(session))
            self.rejected_access_token = None

    async def token(self, expected_scope: str | None = None) -> str:
        async with state_lock(self.directory, "session.lock"):
            session = self.load()
            if session is None:
                raise AuthError("Run yazio-mcp login or import-session locally first")
            if expected_scope is not None and session.scope != expected_scope:
                raise AuthError("The active account changed; the pending operation was not sent")
            if (
                session.expires_at <= time.time() + 60
                or session.access_token == self.rejected_access_token
            ):
                if not session.refresh_token:
                    raise AuthError("This session expired and cannot renew itself; sign in again")
                renewed = await self.exchange(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": session.refresh_token,
                    },
                    session,
                )
                await self.verify(renewed)
                save_private_json(self.path, asdict(renewed))
                session = renewed
                self.rejected_access_token = None
            self.last_access_token = session.access_token
            return session.access_token

    def invalidate(self, access_token: str | None = None) -> None:
        self.rejected_access_token = access_token or self.last_access_token

    def status(self) -> dict[str, Any]:
        session = self.load()
        if session is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "expires_at": session.expires_at,
            "refreshable": bool(session.refresh_token),
            "expired": session.expires_at <= time.time(),
        }

    async def logout(self) -> None:
        async with state_lock(self.directory, "session.lock"):
            self.path.unlink(missing_ok=True)
            self.rejected_access_token = None
            self.last_access_token = None

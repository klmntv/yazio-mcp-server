"""Use the official Apple login page; capture only the resulting YAZIO session."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import subprocess
import tempfile
from importlib.resources import files

from .auth import AuthError
from .local_auth import SessionManager
from .state import private_directory, private_json


def _run(command: list[str], environment: dict[str, str]) -> str:
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=70)
    if result.returncode:
        # Playwriter errors can include a page URL with authorization parameters.
        raise AuthError("Browser login failed; check the browser connection and YAZIO page")
    return result.stdout


async def apple_login(
    manager: SessionManager,
    wrapper: str,
    endpoint: str,
    *,
    show: bool = False,
    web_address: str | None = None,
    timeout: int = 600,
) -> None:
    if not wrapper or not endpoint:
        raise AuthError(
            "Apple login needs a Playwriter wrapper and an explicit owned-browser CDP endpoint"
        )
    if web_address is not None:
        try:
            if not ipaddress.ip_address(web_address).is_global:
                raise ValueError
        except ValueError:
            raise AuthError("The optional web DNS address must be a public literal IP") from None
    private_directory(manager.directory)
    environment = {
        **os.environ,
        "PLAYWRITER_BROWSER_MODE": "direct",
        "PLAYWRITER_CDP_ENDPOINT": endpoint,
    }
    session_id = None
    with tempfile.TemporaryDirectory(prefix="browser-login-", dir=manager.directory) as temporary:
        from pathlib import Path

        directory = Path(temporary)
        output = directory / "candidate.json"
        script = directory / "login.js"
        configuration = {"outputFile": str(output), "show": show, "webAddress": web_address}
        source = files("yazio_mcp").joinpath("browser_login.js").read_text()
        script.write_text(
            "const YAZIO_LOGIN_OPTIONS = " + json.dumps(configuration) + ";\n" + source
        )
        script.chmod(0o600)
        try:
            created = await asyncio.to_thread(_run, [wrapper, "session", "new"], environment)
            match = re.search(r"Session (\d+) created \(direct CDP\)", created)
            if not match:
                raise AuthError("The browser session did not use direct CDP")
            session_id = match.group(1)
            await asyncio.to_thread(
                _run,
                [
                    wrapper,
                    "-s",
                    session_id,
                    "--timeout",
                    "60000",
                    "-f",
                    str(script),
                ],
                environment,
            )
            print(
                "Official YAZIO login is ready. Choose Sign in with Apple in the task tab.",
                flush=True,
            )
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if output.exists():
                    candidate = private_json(output)
                    if candidate is not None:
                        await manager.import_session(candidate)
                        print(
                            "Apple sign-in complete. YAZIO account verified; session saved locally."
                        )
                        return
                await asyncio.sleep(1)
            raise AuthError("Timed out waiting for Apple sign-in; no login state was replaced")
        finally:
            if session_id is not None:
                cleanup = [
                    [
                        wrapper,
                        "-s",
                        session_id,
                        "-e",
                        "for (const p of state.yazioLogin?.ownedPages ?? []) "
                        "{ if (!p.isClosed()) await p.close(); }",
                    ],
                    [wrapper, "session", "delete", session_id],
                ]
                for command in cleanup:
                    try:
                        await asyncio.to_thread(_run, command, environment)
                    except (AuthError, OSError, subprocess.TimeoutExpired):
                        logging.getLogger(__name__).warning(
                            "Browser cleanup incomplete; close only the task-owned login tab"
                        )

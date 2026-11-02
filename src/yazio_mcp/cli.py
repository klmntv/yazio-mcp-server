"""Local CLI for login and a stdio MCP server, without credentials in config."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys

from . import config
from .auth import AuthError
from .config import DEFAULT_HEADERS
from .local_auth import SessionManager
from .server import build_server
from .state import state_directory


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="yazio-mcp")
    commands = result.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in locally; store tokens, not the password")
    login.add_argument("--username", help="YAZIO email address; prompted when omitted")
    login.add_argument("--method", choices=("password", "apple"), default="password")
    login.add_argument("--playwriter", default=os.environ.get("YAZIO_PLAYWRITER"))
    login.add_argument("--cdp-endpoint", default=os.environ.get("PLAYWRITER_CDP_ENDPOINT"))
    login.add_argument("--show-browser", action="store_true", help="Show the task-owned login tab")
    login.add_argument("--web-address", help="Optional public DNS address for www.yazio.com")
    imported = commands.add_parser("import-session", help="Import a YAZIO token JSON via stdin")
    imported.add_argument("--file", help="Read private token JSON from a file instead of stdin")
    serve = commands.add_parser("serve", help="Run local stdio MCP using the saved session")
    serve.add_argument("--allow-writes", action="store_true", help="Expose mutation tools")
    serve.add_argument("--language", default="ru-RU,ru;q=0.9,en;q=0.5")
    serve.add_argument("--timezone", default="Europe/Moscow")
    for command in (
        login,
        imported,
        serve,
        commands.add_parser("status"),
        commands.add_parser("logout"),
    ):
        command.add_argument("--state-dir", help="Private local state directory")
    return result


async def authentication(args, manager: SessionManager) -> None:
    if args.command == "login":
        if args.method == "apple":
            from .browser_login import apple_login

            await apple_login(
                manager,
                args.playwriter,
                args.cdp_endpoint,
                show=args.show_browser,
                web_address=args.web_address,
            )
            return
        if not sys.stdin.isatty():
            raise AuthError("Run login in an interactive terminal; passwords must not be echoed")
        username = args.username or input("YAZIO email: ").strip()
        password = getpass.getpass("YAZIO password: ")
        if not username or not password:
            raise AuthError("Email and password are required")
        await manager.login(username, password)
        print("Signed in. Only session tokens were saved locally.")
    elif args.command == "import-session":
        from .state import private_json

        try:
            if args.file:
                from pathlib import Path

                data = private_json(Path(args.file).expanduser())
            else:
                data = json.load(sys.stdin)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, OSError):
            raise AuthError("Read a token JSON object via stdin or a private 0600 file") from None
        await manager.import_session(data)
        print("YAZIO account verified. Session saved locally.")
    elif args.command == "logout":
        await manager.logout()
        print("Local session removed.")


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    directory = state_directory(args.state_dir)
    manager = SessionManager(directory)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "serve":
            from zoneinfo import ZoneInfo

            ZoneInfo(args.timezone)
            config.APP_TIMEZONE = args.timezone
            DEFAULT_HEADERS["accept-language"] = args.language
            server = build_server(
                session_manager=manager,
                state_dir=directory,
                read_only=not args.allow_writes,
            )
            server.run(transport="stdio")
        elif args.command == "status":
            print(json.dumps(manager.status()))
        else:
            asyncio.run(authentication(args, manager))
    except (AuthError, ValueError, OSError, TimeoutError) as exc:
        print(f"YAZIO: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

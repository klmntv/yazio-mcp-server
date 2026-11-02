"""Runs the YAZIO MCP server over streamable HTTP.

    python3 -m yazio_mcp --host 127.0.0.1 --port 8931

Clients connect to http://<host>:<port>/mcp and authenticate with the same
username and password they use to log in to YAZIO, sent as HTTP Basic
credentials.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from .server import build_app


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {
        "serve", "login", "import-session", "status", "logout",
    }:
        from .cli import main as local_main

        local_main()
        return
    parser = argparse.ArgumentParser(prog="yazio-mcp", description=__doc__)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "address to bind (default: 127.0.0.1). Credentials travel in a "
            "reversibly encoded header, so bind publicly only behind TLS."
        ),
    )
    # Not 8000: that collides with half the development tooling on a machine,
    # and this server is started by hand against a registered URL, so a quiet
    # port is worth more than a familiar one.
    parser.add_argument(
        "--port", type=int, default=8931, help="port to bind (default: 8931)"
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "additional Host header to accept, repeatable. Loopback is always "
            "allowed; name the public hostname here when running behind a "
            "reverse proxy, or requests are refused as DNS rebinding."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug"),
        help="uvicorn log level (default: info)",
    )
    args = parser.parse_args()

    uvicorn.run(
        build_app(args.allowed_host),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()

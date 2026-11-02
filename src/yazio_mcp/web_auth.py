"""Optional per-request DNS repair; HTTPS identity is always www.yazio.com."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket

from .config import HTTP_TIMEOUT


def public_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    if not address.is_global:
        raise ValueError("The web address must be a public literal IP")
    return str(address)


def post_token(address: str, payload: dict[str, str]) -> tuple[int, dict | None]:
    target = public_address(address)

    class Connection(http.client.HTTPSConnection):
        def connect(self):
            plain = socket.create_connection((target, 443), timeout=HTTP_TIMEOUT)
            try:
                self.sock = self._context.wrap_socket(plain, server_hostname="www.yazio.com")
            except BaseException:
                plain.close()
                raise

    connection = Connection("www.yazio.com", timeout=HTTP_TIMEOUT)
    try:
        connection.request(
            "POST",
            "/api/auth/token",
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-client-type": "web",
            },
        )
        response = connection.getresponse()
        body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("The YAZIO response is too large")
        try:
            value = json.loads(body)
            return response.status, value if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            return response.status, None
    finally:
        connection.close()

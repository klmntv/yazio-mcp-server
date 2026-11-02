"""A small durable ledger: request IDs identify actions, never food contents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from .state import private_directory


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Ledger:
    def __init__(self, directory: Path):
        private_directory(directory)
        self.path = directory / "operations.sqlite3"
        if self.path.is_symlink():
            raise ToolError("The operation database must not be a symbolic link")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    scope TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    plan TEXT NOT NULL, state TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL,
                    PRIMARY KEY (scope, request_id)
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def existing(self, scope: str, request_id: str, intent: dict) -> dict | None:
        if not request_id.strip() or len(request_id) > 128:
            raise ToolError("request_id must contain 1 to 128 characters and identify this action")
        fingerprint = hashlib.sha256(encoded(intent).encode()).hexdigest()
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ToolError("This request_id was already used with different parameters")
        return dict(row)

    def begin(self, scope: str, request_id: str, intent: dict, plan: dict) -> tuple[dict, bool]:
        fingerprint = hashlib.sha256(encoded(intent).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?,?,?,?,?,?,?)",
                (scope, request_id, fingerprint, encoded(plan), "executing", None, time.time()),
            ).rowcount
        return self.existing(scope, request_id, intent), bool(inserted)

    def finish(self, scope: str, request_id: str, result: dict) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE operations SET state=?, result=?, updated_at=? "
                "WHERE scope=? AND request_id=? AND state != 'committed'",
                (result["status"], encoded(result), time.time(), scope, request_id),
            )

    def get_operation(self, scope: str, request_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT state, result FROM operations WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
        if row is None:
            raise ToolError("No operation with this request_id belongs to this account")
        return (
            json.loads(row["result"])
            if row["result"]
            else {
                "request_id": request_id,
                "status": row["state"],
                "verified": False,
            }
        )

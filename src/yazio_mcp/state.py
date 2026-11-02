"""Private local state and a cross-process lock for rotating login tokens."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


def state_directory(value: str | Path | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser().absolute()
    configured = os.environ.get("YAZIO_STATE_DIR")
    if configured:
        return Path(configured).expanduser().absolute()
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "yazio-mcp"


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("The state directory must not be a symbolic link")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def private_json(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Session state must be a regular file owned by this user")
        if info.st_mode & 0o077:
            raise ValueError("Session state must have permissions 0600")
        if info.st_size > 1_000_000:
            raise ValueError("Session state is too large")
        try:
            value = json.load(stream)
        except (ValueError, UnicodeError):
            raise ValueError("Session state is invalid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Session state must be a JSON object")
    return value


def save_private_json(path: Path, value: dict[str, Any]) -> None:
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Session state must not be a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@asynccontextmanager
async def state_lock(directory: Path, name: str, timeout: float = 45.0):
    private_directory(directory)
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another process is updating this session") from None
                await asyncio.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

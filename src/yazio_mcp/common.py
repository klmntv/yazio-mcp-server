"""Small helpers shared by the tool modules."""

from __future__ import annotations

import math
from datetime import date as Date
from datetime import datetime as DateTime
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.types import UNSET, Unset

from . import config
from .config import DAYTIMES


def positive_number(value: float, label: str = "amount") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{label} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ToolError(f"{label} must be a positive finite number")
    return float(value)


def local_now():
    return DateTime.now(ZoneInfo(config.APP_TIMEZONE))


def resolve_date(value: str | None) -> str:
    """Normalise a date argument to the `YYYY-MM-DD` form the API expects.

    `None` means today, which is what a caller almost always wants and saves the
    model from having to know the current date to log a meal.
    """
    if value is None:
        return local_now().date().isoformat()

    try:
        return Date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ToolError(
            f"'{value}' is not a valid date; use YYYY-MM-DD, or omit it for today"
        ) from exc


def resolve_timestamp(value: str | None) -> str:
    """Build the `YYYY-MM-DD HH:MM:SS` stamp a written diary entry carries.

    Reads are addressed by plain date, but a consumed item stores a full local
    timestamp — the app records when you ate, not just what day. Logging against
    today therefore uses the current time, while logging against another day
    uses midday: it puts the entry on the right date under any timezone the app
    later renders it in, without pretending to a precision we do not have.
    """
    day = Date.fromisoformat(resolve_date(value))
    now = local_now()

    if day == now.date():
        moment = now
    else:
        moment = DateTime(day.year, day.month, day.day, 12, 0, 0)

    return moment.strftime("%Y-%m-%d %H:%M:%S")


def resolve_range(
    start: str | None, end: str | None, default_days: int = 7
) -> tuple[str, str]:
    """Normalise a start/end pair, defaulting to the last `default_days` days."""
    end_date = Date.fromisoformat(resolve_date(end))
    if start is None:
        start_date = end_date - timedelta(days=default_days - 1)
    else:
        start_date = Date.fromisoformat(resolve_date(start))

    if start_date > end_date:
        raise ToolError(f"start ({start_date}) is after end ({end_date})")

    return start_date.isoformat(), end_date.isoformat()


def resolve_daytime(value: str) -> str:
    """Validate a meal slot.

    Checked here rather than left to the API because YAZIO accepts an unknown
    `daytime` without complaint and then files the entry somewhere the user will
    not find it.
    """
    normalised = value.strip().lower()
    if normalised not in DAYTIMES:
        raise ToolError(f"daytime must be one of {', '.join(DAYTIMES)}; got '{value}'")
    return normalised


def plain(value: Any) -> Any:
    """Convert a generated model's `Unset`/sentinel values into plain JSON.

    The generated attrs models use `UNSET` for absent fields and wrap nullable
    objects in single-attribute classes. Neither survives JSON serialisation, so
    tool results are passed through this first.
    """
    if isinstance(value, Unset) or value is UNSET:
        return None
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if hasattr(value, "to_dict"):
        return plain(value.to_dict())
    return value


def round_floats(value: Any, digits: int = 2) -> Any:
    """Round floats recursively.

    YAZIO returns full double precision, so an untouched day summary is full of
    values like 2143.0000000000005. Rounding keeps results readable without
    changing any number a caller would act on.

    Two decimals suit energy and macros but are coarser than a micronutrient:
    a per-gram vitamin content would round to a flat zero, which reads as "this
    food contains none" rather than "too small to show". Values that would
    vanish that way fall back to three significant digits instead.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, digits)
        if rounded == 0.0 and value != 0.0:
            return float(f"{value:.3g}")
        return rounded
    if isinstance(value, dict):
        return {key: round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [round_floats(item, digits) for item in value]
    return value

"""Date, daytime and payload helpers."""

from __future__ import annotations

from datetime import datetime as DateTime
from zoneinfo import ZoneInfo

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.types import UNSET

from yazio_mcp import config
from yazio_mcp.common import (
    plain,
    resolve_date,
    resolve_daytime,
    resolve_range,
    resolve_timestamp,
    round_floats,
)


def test_omitted_date_means_today():
    assert resolve_date(None) == DateTime.now(ZoneInfo(config.APP_TIMEZONE)).date().isoformat()


def test_passes_through_an_iso_date():
    assert resolve_date("2026-08-02") == "2026-08-02"


@pytest.mark.parametrize("value", ["02.08.2026", "yesterday", "2026-13-01", ""])
def test_rejects_dates_it_cannot_parse(value):
    with pytest.raises(ToolError, match="not a valid date"):
        resolve_date(value)


def test_a_written_entry_carries_a_timestamp():
    """Consumed items store when you ate, not just the day."""
    stamp = resolve_timestamp("2026-08-02")
    assert stamp.startswith("2026-08-02 ")
    assert len(stamp) == len("2026-08-02 12:00:00")


def test_a_past_day_is_stamped_at_midday():
    assert resolve_timestamp("2020-01-01") == "2020-01-01 12:00:00"


def test_today_is_stamped_with_the_current_time():
    stamp = resolve_timestamp(None)
    assert stamp.startswith(DateTime.now(ZoneInfo(config.APP_TIMEZONE)).date().isoformat())


def test_a_range_defaults_to_the_last_week():
    start, end = resolve_range(None, "2026-08-07")
    assert (start, end) == ("2026-08-01", "2026-08-07")


def test_a_range_ends_today_by_default():
    _, end = resolve_range("2026-01-01", None)
    assert end == DateTime.now(ZoneInfo(config.APP_TIMEZONE)).date().isoformat()


def test_an_inverted_range_is_rejected():
    with pytest.raises(ToolError, match="is after"):
        resolve_range("2026-08-07", "2026-08-01")


def test_a_single_day_range_is_allowed():
    assert resolve_range("2026-08-02", "2026-08-02") == ("2026-08-02", "2026-08-02")


@pytest.mark.parametrize("value", ["breakfast", "LUNCH", " dinner ", "snack"])
def test_accepts_the_four_meal_slots(value):
    assert resolve_daytime(value) == value.strip().lower()


def test_rejects_an_unknown_meal_slot():
    """YAZIO accepts a bogus daytime silently and then hides the entry."""
    with pytest.raises(ToolError, match="daytime must be one of"):
        resolve_daytime("brunch")


def test_unset_becomes_none():
    assert plain(UNSET) is None


def test_unset_is_cleared_recursively():
    assert plain({"a": UNSET, "b": [UNSET, 1]}) == {"a": None, "b": [None, 1]}


def test_models_are_flattened_via_to_dict():
    class Model:
        def to_dict(self):
            return {"value": UNSET}

    assert plain(Model()) == {"value": None}


def test_floats_are_rounded():
    assert round_floats({"energy": 2143.0000000000005}) == {"energy": 2143.0}


def test_rounding_reaches_into_lists_and_dicts():
    assert round_floats([{"a": 1.23456}]) == [{"a": 1.23}]


def test_small_values_survive_rounding():
    """A trace nutrient must not be reported as an absent one."""
    assert round_floats({"b12": 4.9e-06}) == {"b12": 4.9e-06}
    assert round_floats({"k": 0.0000615432}) == {"k": 6.15e-05}


def test_zero_stays_zero():
    assert round_floats({"alcohol": 0.0}) == {"alcohol": 0.0}


def test_rounding_leaves_booleans_alone():
    """bool is a subclass of int; rounding must not turn True into 1."""
    assert round_floats({"tracked": True}) == {"tracked": True}


def test_rounding_leaves_strings_and_none_alone():
    assert round_floats({"a": "x", "b": None}) == {"a": "x", "b": None}

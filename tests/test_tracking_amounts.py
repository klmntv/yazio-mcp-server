"""Resolving a portion to a base-unit amount.

This is the arithmetic that decides what actually gets written to someone's
diary, and the same rules feed recipe ingredients, so it is worth pinning down
away from the network.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yazio_mcp.tools.recipes import _nutrients_for_amount
from yazio_mcp.tools.tracking import _resolve_amount, _serving_fields

OLIVE_OIL = {
    "name": "Olivenöl",
    "base_unit": "g",
    "servings": [{"serving": "tablespoon", "amount": 15.0}],
    # Per one base unit: olive oil really is 8.84 kcal per gram.
    "nutrients": {"energy.energy": 8.84, "nutrient.fat": 1.0},
}


def test_an_explicit_amount_is_taken_as_given():
    assert _resolve_amount(OLIVE_OIL, 250, None, None) == 250.0


@pytest.mark.parametrize("amount", [-100, 0, float("nan"), float("inf")])
def test_invalid_explicit_amounts_are_refused(amount):
    with pytest.raises(ToolError, match="positive finite"):
        _resolve_amount(OLIVE_OIL, amount, None, None)


def test_an_explicit_amount_wins_over_a_serving():
    assert _resolve_amount(OLIVE_OIL, 250, "tablespoon", 1) == 250.0


def test_a_named_serving_resolves_to_base_units():
    assert _resolve_amount(OLIVE_OIL, None, "tablespoon", 1) == 15.0


def test_serving_quantity_multiplies():
    assert _resolve_amount(OLIVE_OIL, None, "tablespoon", 3) == 45.0


def test_a_missing_quantity_means_one():
    assert _resolve_amount(OLIVE_OIL, None, "tablespoon", None) == 15.0


def test_a_fractional_quantity_is_allowed():
    assert _resolve_amount(OLIVE_OIL, None, "tablespoon", 0.5) == 7.5


def test_an_unknown_serving_is_refused_with_the_alternatives():
    """Guessing here would silently log the wrong quantity."""
    with pytest.raises(ToolError, match="tablespoon"):
        _resolve_amount(OLIVE_OIL, None, "cup", 1)


def test_a_product_without_servings_demands_an_amount():
    with pytest.raises(ToolError, match="no servings"):
        _resolve_amount({"name": "x"}, None, "slice", 1)


def test_serving_fields_are_omitted_for_a_raw_amount():
    """A null serving makes the app show the entry without a portion label."""
    assert _serving_fields(None, None) == {}


def test_serving_fields_default_the_quantity():
    assert _serving_fields("can", None) == {"serving": "can", "serving_quantity": 1.0}


def test_nutrients_scale_by_the_amount_used():
    """Nutrients are stored per base unit, so the factor is the amount itself."""
    scaled = _nutrients_for_amount(OLIVE_OIL, 100)
    assert scaled["energy.energy"] == pytest.approx(884.0)
    assert scaled["nutrient.fat"] == pytest.approx(100.0)


def test_one_base_unit_is_the_unscaled_table():
    assert _nutrients_for_amount(OLIVE_OIL, 1)["energy.energy"] == pytest.approx(8.84)


def test_a_product_without_nutrients_cannot_be_an_ingredient():
    with pytest.raises(ToolError, match="no nutrient data"):
        _nutrients_for_amount({"name": "x", "nutrients": {}}, 100)

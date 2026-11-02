"""Native saved Meals: preserve exact products and guard one-off changes."""

import copy
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk import AuthenticatedClient

from yazio_mcp.config import BASE_URL
from yazio_mcp.server import build_server
from yazio_mcp.tools import meals

MEAL = {
    "id": "meal-fixture",
    "name": "Breakfast",
    "products": [{"product_id": "milk", "amount": 200, "serving": "glass", "serving_quantity": 1}],
    "recipe_portions": [{"recipe_id": "omelet", "portion_count": 0.5}],
    "simple_products": [{"name": "Coffee", "nutrients": {"energy.energy": 5, "nutrient.fat": 0}}],
}


@pytest.mark.asyncio
@respx.mock
async def test_reads_native_meals_from_the_live_contract_without_local_presets(monkeypatch):
    route = respx.get(f"{BASE_URL}/v22/user/meals").mock(
        return_value=httpx.Response(200, json=[MEAL])
    )

    @asynccontextmanager
    async def client(_ctx):
        async with AuthenticatedClient(base_url=BASE_URL, token="test-only-token") as value:
            yield value

    monkeypatch.setattr(meals, "yazio_client", client)
    assert await meals.load_meals(object()) == [MEAL]
    assert route.call_count == 1


def test_builds_all_three_food_buckets_and_scales_portions():
    original = copy.deepcopy(MEAL)
    result = meals.consumption_items(MEAL, 2, [], {})
    assert result[0]["entry"]["product_id"] == "milk"
    assert result[0]["entry"]["amount"] == 400
    assert result[0]["entry"]["serving_quantity"] == 2
    assert result[1]["entry"]["portion_count"] == 1
    assert result[2]["entry"]["nutrients"]["energy.energy"] == 10
    assert MEAL == original


def test_an_amount_override_clears_the_old_serving_label_and_can_omit_foods():
    result = meals.consumption_items(MEAL, 1, ["recipe_portions:0"], {"products:0": 150})
    assert result[0]["entry"] == {"product_id": "milk", "amount": 150}
    assert len(result) == 2


def test_duplicate_names_need_an_explicit_id():
    with pytest.raises(ToolError, match="ambiguous"):
        meals.select_meal([MEAL, {**MEAL, "id": "other-meal"}], "breakfast")
    assert meals.select_meal([MEAL], "meal-fixture") == MEAL


@pytest.mark.parametrize("amount", [-10, 0, float("nan"), float("inf")])
def test_invalid_amount_overrides_are_refused(amount):
    with pytest.raises(ToolError, match="positive finite"):
        meals.consumption_items(MEAL, 1, [], {"products:0": amount})


def test_a_changed_template_has_a_new_version():
    changed = copy.deepcopy(MEAL)
    changed["products"][0]["amount"] = 150
    assert meals.version(changed) != meals.version(MEAL)


def test_unknown_item_keys_are_refused():
    with pytest.raises(ToolError, match="no longer matches"):
        meals.consumption_items(MEAL, 1, ["products:99"], {})


@pytest.mark.asyncio
async def test_changed_native_meal_blocks_an_old_item_override(monkeypatch):
    changed = copy.deepcopy(MEAL)
    changed["products"][0]["amount"] = 175

    async def current(_ctx):
        return [changed]

    async def run_plan(_ctx, _request, _intent, factory, _date, _slot):
        await factory()
        raise AssertionError("An outdated override must fail before a write plan is accepted")

    monkeypatch.setattr(meals, "load_meals", current)
    monkeypatch.setattr(meals, "record_consumption", run_plan)
    with pytest.raises(ToolError, match="changed in YAZIO"):
        await build_server().call_tool(
            "track_saved_meal",
            {
                "meal": "Breakfast",
                "daytime": "breakfast",
                "request_id": "change-one-item",
                "amounts": {"products:0": 150},
                "expected_version": meals.version(MEAL),
            },
        )


@pytest.mark.asyncio
async def test_an_override_requires_a_version_before_loading_or_writing(monkeypatch):
    async def unexpected(_ctx):
        raise AssertionError("A missing version must be rejected before accessing the account")

    monkeypatch.setattr(meals, "load_meals", unexpected)
    with pytest.raises(ToolError, match="expected_version"):
        await build_server().call_tool(
            "track_saved_meal",
            {
                "meal": "Breakfast",
                "daytime": "breakfast",
                "request_id": "omit-one-item",
                "omit": ["products:0"],
            },
        )

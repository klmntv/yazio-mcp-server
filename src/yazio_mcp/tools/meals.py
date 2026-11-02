"""Native YAZIO saved Meals, read fresh rather than copied into local presets."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..common import positive_number, resolve_daytime
from ..consumption import record_consumption
from ..ledger import Ledger, encoded
from ..session import _invalidate, data_directory, operation_scope, yazio_client
from .products import fetch_product

BUCKETS = ("products", "recipe_portions", "simple_products")


async def load_meals(ctx: Context) -> list[dict]:
    async with yazio_client(ctx) as client:
        response = await client.get_async_httpx_client().get("/v22/user/meals")
        if response.status_code == 401:
            _invalidate(ctx)
    if response.status_code != 200:
        raise ToolError(f"Unable to read saved YAZIO meals (HTTP {response.status_code})")
    try:
        values = response.json()
        if not isinstance(values, list) or any(
            not isinstance(meal, dict)
            or not isinstance(meal.get("id"), str)
            or not isinstance(meal.get("name"), str)
            for meal in values
        ):
            raise ValueError
        return values
    except (ValueError, TypeError):
        raise ToolError("YAZIO returned an unsupported saved-meal format") from None


def version(meal: dict) -> str:
    return hashlib.sha256(encoded(meal).encode()).hexdigest()


def select_meal(values: list[dict], key: str) -> dict:
    by_id = [meal for meal in values if meal["id"].lower() == key.lower()]
    matches = by_id or [
        meal for meal in values if meal["name"].strip().casefold() == key.strip().casefold()
    ]
    if len(matches) != 1:
        raise ToolError(
            "Select one saved YAZIO meal by its exact name or ID; the match is missing or ambiguous"
        )
    return matches[0]


def meal_items(meal: dict) -> list[dict]:
    result = []
    for bucket in BUCKETS:
        rows = meal.get(bucket) or []
        if not isinstance(rows, list):
            raise ToolError("YAZIO returned an unsupported saved-meal item format")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ToolError("YAZIO returned an unsupported saved-meal item format")
            result.append({"item_key": f"{bucket}:{index}", "bucket": bucket, "entry": dict(row)})
    return result


def consumption_items(
    meal: dict,
    multiplier: float,
    omit: list[str],
    amounts: dict[str, float],
) -> list[dict]:
    positive_number(multiplier, "multiplier")
    rows = meal_items(meal)
    known = {item["item_key"] for item in rows}
    if (set(omit) | set(amounts)) - known:
        raise ToolError("An item override no longer matches this saved meal")
    result = []
    for item in rows:
        key, bucket, source = item["item_key"], item["bucket"], item["entry"]
        if key in omit:
            continue
        if bucket == "products":
            product_id = source.get("product_id")
            if not isinstance(product_id, str) or not product_id:
                raise ToolError("A saved meal product has no product ID")
            amount = positive_number(amounts.get(key, source.get("amount")), "amount")
            entry = {"product_id": product_id, "amount": positive_number(amount * multiplier)}
            if key not in amounts and source.get("serving"):
                entry["serving"] = source["serving"]
                if source.get("serving_quantity") is not None:
                    entry["serving_quantity"] = positive_number(
                        positive_number(source["serving_quantity"], "serving_quantity")
                        * multiplier,
                        "serving_quantity",
                    )
        elif bucket == "recipe_portions":
            recipe_id = source.get("recipe_id")
            if not isinstance(recipe_id, str) or not recipe_id:
                raise ToolError("A saved meal recipe has no recipe ID")
            portions = positive_number(amounts.get(key, source.get("portion_count")), "portions")
            entry = {
                "recipe_id": recipe_id,
                "portion_count": positive_number(portions * multiplier),
            }
        else:
            if key in amounts:
                raise ToolError(
                    "Quick-add entries have no gram amount; use multiplier or omit them"
                )
            nutrients = source.get("nutrients")
            if not isinstance(nutrients, dict) or not nutrients:
                raise ToolError("A quick-add saved meal item has no nutrition data")
            scaled = {}
            for nutrient, number in nutrients.items():
                if isinstance(number, bool) or not isinstance(number, (int, float)):
                    raise ToolError("A quick-add nutrient is not numeric")
                value = number * multiplier
                if not math.isfinite(value) or value < 0:
                    raise ToolError("A quick-add nutrient is not a nonnegative finite number")
                scaled[nutrient] = value
            entry = {
                "name": str(source.get("name", "")),
                "type": "simple_product",
                "nutrients": scaled,
            }
        result.append({"bucket": bucket, "entry": entry})
    if not result:
        raise ToolError("No foods remain in this meal")
    return result


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def list_saved_meals(ctx: Context) -> dict[str, Any]:
        """List the user's existing saved Meals directly from YAZIO, with names and IDs.

        These are the same saved food combinations shown in the app, not local presets.
        """
        meals = await load_meals(ctx)
        return {
            "source": "yazio",
            "meals": [
                {
                    "id": meal["id"],
                    "name": meal["name"],
                    "version": version(meal),
                    "item_count": len(meal_items(meal)),
                }
                for meal in meals
            ],
        }

    @mcp.tool()
    async def get_saved_meal(ctx: Context, meal: str) -> dict[str, Any]:
        """Read one saved YAZIO Meal by exact name or ID, including amounts and item keys.

        Use its version and item_key values for safe one-off omissions or amount overrides.
        """
        selected = select_meal(await load_meals(ctx), meal)
        items = meal_items(selected)
        async with yazio_client(ctx) as client:
            for item in items:
                if item["bucket"] == "products":
                    product = await fetch_product(client, item["entry"].get("product_id", ""))
                    if product:
                        item["name"] = product.get("name")
                        item["base_unit"] = product.get("base_unit")
        return {
            "source": "yazio",
            "id": selected["id"],
            "name": selected["name"],
            "version": version(selected),
            "items": items,
        }

    @mcp.tool()
    async def track_saved_meal(
        ctx: Context,
        meal: str,
        daytime: str,
        request_id: str,
        date: str | None = None,
        multiplier: float = 1,
        omit: list[str] | None = None,
        amounts: dict[str, float] | None = None,
        expected_version: str | None = None,
    ) -> dict[str, Any]:
        """Record an existing saved YAZIO Meal using the durable verified write path.

        request_id identifies one user action; reuse it after timeouts. New identical
        consumption needs a NEW request_id. amount overrides use base units for products,
        portions for recipes. Changes apply to this consumption only, not the saved Meal.
        Omissions/overrides require expected_version from get_saved_meal.
        """
        slot = resolve_daytime(daytime)
        positive_number(multiplier, "multiplier")
        omit, amounts = omit or [], amounts or {}
        for value in amounts.values():
            positive_number(value)
        if (omit or amounts) and not expected_version:
            raise ToolError("Read the saved meal first and pass expected_version for overrides")

        async def plan():
            selected = select_meal(await load_meals(ctx), meal)
            if expected_version and expected_version != version(selected):
                raise ToolError(
                    "The saved Meal changed in YAZIO; read it again before recording changes"
                )
            return consumption_items(selected, multiplier, omit, amounts)

        return await record_consumption(
            ctx,
            request_id,
            {
                "action": "track_saved_meal",
                "meal": meal,
                "date": date,
                "daytime": slot,
                "multiplier": float(multiplier),
                "omit": sorted(omit),
                "amounts": amounts,
                "expected_version": expected_version,
            },
            plan,
            date,
            slot,
        )

    @mcp.tool()
    async def get_operation(ctx: Context, request_id: str) -> dict[str, Any]:
        """Read the saved result of a food-recording operation for the current account."""
        return Ledger(data_directory(ctx)).get_operation(operation_scope(ctx), request_id)

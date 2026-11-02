"""Reading what was eaten, and how it compares to the day's goals."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from yazio_sdk.api.activity import (
    get_daily_exercise_summary as api_get_daily_exercise_summary,
)
from yazio_sdk.api.diary import get_daily_nutrients as api_get_daily_nutrients
from yazio_sdk.api.diary import get_water_intake as api_get_water_intake
from yazio_sdk.api.diary import list_consumed_items as api_list_consumed_items
from yazio_sdk.api.goals import get_goals as api_get_goals
from yazio_sdk.api.widgets import (
    get_daily_summary_widget as api_get_daily_summary_widget,
)

from ..common import plain, resolve_date, resolve_range, round_floats
from ..config import DAYTIMES
from ..nutrients import group_nutrients
from ..session import expect_ok, yazio_client
from .products import fetch_product


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_daily_summary(
        ctx: Context, date: str | None = None
    ) -> dict[str, Any]:
        """Get the full picture of one day: goals, intake per meal, water, activity.

        This is the tool to reach for when asked how a day is going or how it
        went. One call covers energy and macros consumed against goal, the split
        across breakfast/lunch/dinner/snack, water intake, steps and exercise
        energy, plus current and starting weight.

        Args:
            date: Day to summarise as YYYY-MM-DD. Defaults to today.
        """
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_get_daily_summary_widget.asyncio_detailed(
                client=client, date=day
            )
            summary = expect_ok(ctx, response, f"load the summary for {day}")

        goals = plain(summary.goals) or {}
        meals = plain(summary.meals) or {}
        units = plain(summary.units) or {}
        user = plain(summary.user) or {}

        consumed = _sum_meals(meals)
        energy_goal = goals.get("energy.energy")

        return round_floats(
            {
                "date": day,
                "energy": _progress(consumed.get("energy.energy"), energy_goal),
                "macros": {
                    macro: _progress(consumed.get(key), goals.get(key))
                    for macro, key in (
                        ("carb", "nutrient.carb"),
                        ("protein", "nutrient.protein"),
                        ("fat", "nutrient.fat"),
                    )
                },
                "meals": {
                    slot: _shape_meal(meals.get(slot) or {}) for slot in DAYTIMES
                },
                "water": _progress(
                    plain(summary.water_intake), goals.get("water"), "ml"
                ),
                "activity": {
                    "energy_burned_kcal": plain(summary.activity_energy),
                    "steps": _progress(
                        plain(summary.steps), goals.get("activity.step"), "steps"
                    ),
                    "counts_towards_budget": plain(summary.consume_activity_energy),
                },
                "weight": {
                    "current_kg": user.get("current_weight"),
                    "start_kg": user.get("start_weight"),
                    "goal_kg": goals.get("bodyvalue.weight"),
                    "direction": user.get("goal"),
                },
                "display_units": units,
            }
        )

    @mcp.tool()
    async def get_diary(ctx: Context, date: str | None = None) -> dict[str, Any]:
        """List the individual items logged on a day, grouped by meal.

        Use this when the question is *what* was eaten rather than how much —
        for example before removing or correcting an entry, since each item
        comes back with the entry_id that untrack_item needs.
        Product amounts are expressed in each item's base_unit. Preserve that
        unit even for drinks: grams and millilitres are not interchangeable.

        Args:
            date: Day to read as YYYY-MM-DD. Defaults to today.
        """
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_list_consumed_items.asyncio_detailed(
                client=client, date=day
            )
            items = expect_ok(ctx, response, f"load the diary for {day}")

            products = plain(items.products) or []

            # Diary entries reference products by id only. Resolving them is
            # what turns the result from a list of UUIDs into something a reader
            # can act on, so it is worth the fan-out; the calls run concurrently
            # because a busy day can hold a dozen entries.
            unique_ids = list(
                {item["product_id"] for item in products if item.get("product_id")}
            )
            details = await asyncio.gather(
                *(fetch_product(client, product_id) for product_id in unique_ids)
            )

        # strict: `details` is gathered from `unique_ids`, so a length mismatch
        # would mean the gather above lost a result rather than a short zip.
        by_id = dict(zip(unique_ids, details, strict=True))
        meals: dict[str, list[dict[str, Any]]] = {slot: [] for slot in DAYTIMES}

        for item in products:
            slot = item.get("daytime")
            entry = {
                "entry_id": item.get("id"),
                "product_id": item.get("product_id"),
                "amount": item.get("amount"),
                "serving": item.get("serving"),
                "serving_quantity": item.get("serving_quantity"),
            }
            detail = by_id.get(item.get("product_id"))
            if detail is not None:
                entry["name"] = detail.get("name")
                entry["producer"] = detail.get("producer")
                entry["base_unit"] = detail.get("base_unit")

            meals.setdefault(slot or "snack", []).append(entry)

        return round_floats(
            {
                "date": day,
                "meals": meals,
                "recipe_portions": plain(items.recipe_portions) or [],
                "simple_products": plain(items.simple_products) or [],
            }
        )

    @mcp.tool()
    async def get_nutrition_range(
        ctx: Context,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Get per-day energy and macros over a date range, with goals.

        Built for questions about trends and averages — "how did I eat this
        week", "am I hitting my protein goal" — rather than about a single day.

        Args:
            start: First day as YYYY-MM-DD. Defaults to 7 days before end.
            end: Last day as YYYY-MM-DD. Defaults to today.
        """
        first, last = resolve_range(start, end)

        async with yazio_client(ctx) as client:
            response = await api_get_daily_nutrients.asyncio_detailed(
                client=client, start=first, end=last
            )
            days = expect_ok(ctx, response, f"load nutrition from {first} to {last}")

        entries = [
            {
                "date": plain(day.date),
                "energy_kcal": plain(day.energy),
                "energy_goal_kcal": plain(day.energy_goal),
                "carb_g": plain(day.carb),
                "protein_g": plain(day.protein),
                "fat_g": plain(day.fat),
            }
            for day in days
        ]

        return round_floats(
            {
                "start": first,
                "end": last,
                "days": entries,
                "averages": _averages(entries),
            }
        )

    @mcp.tool()
    async def get_goals(ctx: Context, date: str | None = None) -> dict[str, Any]:
        """Get the nutrition, water, step and weight goals in force on a day.

        Args:
            date: Day to read goals for as YYYY-MM-DD. Defaults to today.
        """
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_get_goals.asyncio_detailed(client=client, date=day)
            goals = expect_ok(ctx, response, f"load goals for {day}")

        raw = plain(goals) or {}
        return round_floats(
            {
                "date": day,
                "energy_kcal": raw.get("energy.energy"),
                "carb_g": raw.get("nutrient.carb"),
                "protein_g": raw.get("nutrient.protein"),
                "fat_g": raw.get("nutrient.fat"),
                "water_ml": raw.get("water"),
                "steps": raw.get("activity.step"),
                "target_weight_kg": raw.get("bodyvalue.weight"),
            }
        )

    @mcp.tool()
    async def get_water(ctx: Context, date: str | None = None) -> dict[str, Any]:
        """Get water intake for a day, in millilitres, against the daily goal.

        Args:
            date: Day to read as YYYY-MM-DD. Defaults to today.
        """
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            intake_response = await api_get_water_intake.asyncio_detailed(
                client=client, date=day
            )
            intake = expect_ok(ctx, intake_response, f"load water intake for {day}")

            goals_response = await api_get_goals.asyncio_detailed(
                client=client, date=day
            )
            goals = expect_ok(ctx, goals_response, f"load goals for {day}")

        return round_floats(
            {
                "date": day,
                **_progress(
                    plain(intake.water_intake), (plain(goals) or {}).get("water"), "ml"
                ),
            }
        )

    @mcp.tool()
    async def get_activity_summary(
        ctx: Context,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Get per-day steps and exercise energy over a date range.

        Args:
            start: First day as YYYY-MM-DD. Defaults to 7 days before end.
            end: Last day as YYYY-MM-DD. Defaults to today.
        """
        first, last = resolve_range(start, end)

        async with yazio_client(ctx) as client:
            response = await api_get_daily_exercise_summary.asyncio_detailed(
                client=client, start=first, end=last
            )
            days = expect_ok(ctx, response, f"load activity from {first} to {last}")

        return round_floats(
            {"start": first, "end": last, "days": [plain(day) for day in days]}
        )


def _shape_meal(meal: dict[str, Any]) -> dict[str, Any]:
    """Present one meal's totals, against the share of the budget it is given.

    A meal is `{energy_goal, nutrients}` — the nutrients sit one level down, and
    each slot carries its own slice of the day's energy goal.
    """
    shaped = group_nutrients(meal.get("nutrients") or {})
    energy_goal = meal.get("energy_goal")
    if isinstance(energy_goal, (int, float)):
        shaped["energy_goal_kcal"] = energy_goal
    return shaped


def _sum_meals(meals: dict[str, Any]) -> dict[str, float]:
    """Add up the four meal slots into a day total.

    The summary widget reports per-meal totals but no day total, so the headline
    "consumed today" number has to be assembled here.
    """
    total: dict[str, float] = {}
    for meal in meals.values():
        if not isinstance(meal, dict):
            continue
        for key, value in (meal.get("nutrients") or {}).items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0.0) + float(value)
    return total


def _progress(actual: Any, goal: Any, unit: str = "") -> dict[str, Any]:
    """Express a value against its goal, including what is left.

    Returning the remainder alongside the raw numbers keeps a caller from having
    to do arithmetic to answer the question that is actually being asked, which
    is nearly always "how much is left".
    """
    result: dict[str, Any] = {"consumed": actual, "goal": goal}
    if unit:
        result["unit"] = unit

    if isinstance(actual, (int, float)) and isinstance(goal, (int, float)) and goal:
        result["remaining"] = goal - actual
        result["percent_of_goal"] = actual / goal * 100

    return result


def _averages(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean of each numeric field across days that have data."""
    if not entries:
        return {}

    keys = ("energy_kcal", "carb_g", "protein_g", "fat_g")
    averages: dict[str, Any] = {}
    for key in keys:
        values = [e[key] for e in entries if isinstance(e.get(key), (int, float))]
        if values:
            averages[key] = sum(values) / len(values)

    averages["days_counted"] = len(entries)
    return averages

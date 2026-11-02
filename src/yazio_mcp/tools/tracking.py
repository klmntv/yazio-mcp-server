"""Writing to the diary: logging food, water and exercise.

Every tool here changes the user's data. They are deliberately narrow — one
action each, with the resolved result echoed back — so that a caller can tell
exactly what was written without re-reading the diary.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.api.diary import delete_consumed_item as api_delete_consumed_item
from yazio_sdk.api.diary import list_consumed_items as api_list_consumed_items
from yazio_sdk.api.diary import set_water_intake as api_set_water_intake
from yazio_sdk.models import (
    ConsumedItemsDeletion,
    WaterIntakeEntry,
)

from ..common import (
    plain,
    positive_number,
    resolve_date,
    resolve_daytime,
    resolve_timestamp,
)
from ..consumption import record_consumption
from ..session import expect_ok, expect_written, yazio_client
from .products import fetch_product


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def track_product(
        ctx: Context,
        product_id: str,
        daytime: str,
        request_id: str,
        amount: float | None = None,
        serving: str | None = None,
        serving_quantity: float | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Log a product to the diary.

        Give the portion either as a raw `amount` in the product's base unit
        (grams or millilitres), or as a named `serving` plus how many of them —
        for example serving="can", serving_quantity=1. When a serving is named,
        the base-unit amount is looked up from the product so YAZIO records both
        consistently.

        Args:
            product_id: The product's UUID, from search_products.
            request_id: Stable ID for this user action. Reuse it after a timeout.
            daytime: One of breakfast, lunch, dinner, snack.
            amount: Portion size in the product's base unit. Optional if serving is given.
            serving: Named serving such as "can", "slice", "gram".
            serving_quantity: How many of that serving. Defaults to 1 when serving is given.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        slot = resolve_daytime(daytime)

        if amount is None and serving is None:
            raise ToolError("give either an amount, or a serving to log a portion of")

        if amount is not None:
            positive_number(amount)
        if serving_quantity is not None:
            positive_number(serving_quantity, "serving_quantity")

        async def plan():
            async with yazio_client(ctx) as client:
                product = await fetch_product(client, product_id)
                if product is None:
                    raise ToolError(f"no product found with id {product_id}")
                quantity = (
                    serving_quantity if serving_quantity is not None else (1.0 if serving else None)
                )
                return [{"bucket": "products", "entry": {
                    "product_id": product_id,
                    "amount": _resolve_amount(product, amount, serving, quantity),
                    **_serving_fields(serving, quantity),
                }}]

        return await record_consumption(ctx, request_id, {
            "action": "track_product", "product_id": product_id, "daytime": slot,
            "amount": amount, "serving": serving,
            "serving_quantity": serving_quantity, "date": date,
        }, plan, date, slot)

    @mcp.tool()
    async def track_recipe(
        ctx: Context,
        recipe_id: str,
        daytime: str,
        request_id: str,
        portions: float = 1,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Log portions of a recipe to the diary.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes or get_recipe.
            request_id: Stable ID for this user action. Reuse it after a timeout.
            daytime: One of breakfast, lunch, dinner, snack.
            portions: How many portions of the recipe were eaten.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        slot = resolve_daytime(daytime)
        positive_number(portions, "portions")

        async def plan():
            return [{"bucket": "recipe_portions", "entry": {
                "recipe_id": recipe_id, "portion_count": float(portions),
            }}]

        return await record_consumption(ctx, request_id, {
            "action": "track_recipe", "recipe_id": recipe_id, "daytime": slot,
            "portions": float(portions), "date": date,
        }, plan, date, slot)

    @mcp.tool()
    async def untrack_item(
        ctx: Context,
        entry_id: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Remove a logged item from the diary.

        Takes the entry_id that get_diary returns for each item — not the
        product_id, which identifies the food rather than this particular
        logging of it.

        Args:
            entry_id: The diary entry's UUID, from get_diary.
            date: The day the entry is on, as YYYY-MM-DD. Defaults to today.
                Only needed to look the entry up; give it when removing
                something logged on another day.
        """
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            kind = await _classify_entry(ctx, client, entry_id, day)

            response = await api_delete_consumed_item.asyncio_detailed(
                client=client, body=ConsumedItemsDeletion(**{kind: entry_id})
            )
            expect_written(ctx, response, f"remove diary entry {entry_id}")

            # The endpoint answers 204 whether or not it removed anything, so
            # the only way to report honestly is to look again.
            still_there = await _classify_entry(
                ctx, client, entry_id, day, missing_ok=True
            )

        if still_there is not None:
            raise ToolError(
                f"YAZIO accepted the removal of {entry_id} but the entry is still "
                f"on {day}. It may need to be removed in the app."
            )

        return {"removed": True, "entry_id": entry_id, "date": day, "kind": kind}

    @mcp.tool()
    async def log_water(
        ctx: Context,
        millilitres: float,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Set the day's water intake, in millilitres.

        This replaces the day's total rather than adding to it, matching how the
        app's water tracker works. Read get_water first if you mean to add to
        what is already logged.

        Args:
            millilitres: Total water for the day, in ml.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        if millilitres < 0:
            raise ToolError("water intake cannot be negative")

        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_set_water_intake.asyncio_detailed(
                client=client,
                body=[
                    WaterIntakeEntry(
                        date=resolve_timestamp(date), water_intake=millilitres
                    )
                ],
            )
            expect_written(ctx, response, f"log water for {day}")

        return {"logged": True, "date": day, "water_intake_ml": millilitres}


async def _classify_entry(
    ctx: Context,
    client: Any,
    entry_id: str,
    day: str,
    missing_ok: bool = False,
) -> str | None:
    """Find which of the diary's three buckets an entry lives in.

    The deletion request names the bucket — `products`, `recipe_portions` or
    `simple_products` — and sending the wrong one is accepted with a 204 that
    removes nothing, so the entry has to be located first.
    """
    response = await api_list_consumed_items.asyncio_detailed(
        client=client, date=day
    )
    diary = expect_ok(ctx, response, f"find diary entry {entry_id}")

    wanted = entry_id.lower()
    for bucket in ("products", "recipe_portions", "simple_products"):
        for item in plain(getattr(diary, bucket)) or []:
            if isinstance(item, dict) and str(item.get("id", "")).lower() == wanted:
                return bucket

    if missing_ok:
        return None

    raise ToolError(
        f"no diary entry with id {entry_id} on {day}. "
        "Use get_diary to list the entries for that day, and pass its date if "
        "the entry is not on today."
    )


def _serving_fields(serving: str | None, quantity: float | None) -> dict[str, Any]:
    """Build the optional serving half of a consumed-item payload.

    Omitted entirely when logging a raw amount, because sending a null serving
    makes YAZIO show the entry without a portion label.
    """
    if serving is None:
        return {}
    return {
        "serving": serving,
        "serving_quantity": quantity if quantity is not None else 1.0,
    }


def _resolve_amount(
    product: dict[str, Any],
    amount: float | None,
    serving: str | None,
    quantity: float | None,
) -> float:
    """Work out the base-unit amount for a portion.

    An explicit amount always wins. Otherwise the named serving is looked up in
    the product's serving table and multiplied out. If the product does not list
    that serving we refuse rather than guess, because silently logging the wrong
    quantity is worse than asking the caller to pick a serving the product has.
    """
    if amount is not None:
        return positive_number(amount)

    servings = product.get("servings")
    if not isinstance(servings, list):
        raise ToolError(
            f"product {product.get('name', '')} lists no servings; "
            "give an explicit amount in its base unit instead"
        )

    for option in servings:
        if not isinstance(option, dict):
            continue
        if option.get("serving") == serving or option.get("name") == serving:
            base_amount = option.get("amount")
            if isinstance(base_amount, (int, float)):
                return positive_number(
                    positive_number(base_amount, "serving amount") * positive_number(
                        quantity if quantity is not None else 1.0, "serving_quantity"
                    )
                )

    available = sorted(
        str(option.get("serving") or option.get("name"))
        for option in servings
        if isinstance(option, dict) and (option.get("serving") or option.get("name"))
    )
    raise ToolError(
        f"product does not offer a '{serving}' serving. Available: {', '.join(available) or 'none'}"
    )

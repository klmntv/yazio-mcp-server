"""Finding products in YAZIO's food database."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk import AuthenticatedClient
from yazio_sdk.api.products import get_product as api_get_product
from yazio_sdk.api.products import (
    list_suggested_products as api_list_suggested_products,
)
from yazio_sdk.api.products import search_products as api_search_products
from yazio_sdk.api.user import get_user as api_get_user

from ..common import plain, resolve_date, resolve_daytime, round_floats
from ..nutrients import group_nutrients
from ..session import expect_ok, yazio_client

# Used only if the profile does not say. Search rejects a blank country outright,
# so there has to be some value to fall back on.
_FALLBACK_COUNTRY = "DE"
_FALLBACK_SEX = "male"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def search_products(
        ctx: Context,
        query: str,
        limit: int = 20,
        countries: str | None = None,
    ) -> dict[str, Any]:
        """Search YAZIO's food database by name or barcode.

        Accepts free text ("greek yoghurt") as well as a scanned EAN barcode.
        Each result carries the product_id needed to track it, its default
        serving, and per-serving nutrients.

        Results are ranked for the user's own country and language unless a
        different country is named.

        Args:
            query: Product name or barcode to search for.
            limit: Maximum number of results to return.
            countries: Comma-separated country codes to search instead of the
                user's own, such as "US" or "GB,IE".
        """
        if not query.strip():
            raise ToolError("query must not be empty")

        async with yazio_client(ctx) as client:
            # The API rejects a search with a blank `sex` or `countries`, so
            # both have to be supplied even though neither is something a caller
            # should have to think about. Taking them from the profile keeps
            # results ranked the way the app would rank them.
            profile_response = await api_get_user.asyncio_detailed(client=client)
            profile = expect_ok(ctx, profile_response, "read your search region")

            response = await api_search_products.asyncio_detailed(
                client=client,
                query=query.strip(),
                sex=plain(profile.sex) or _FALLBACK_SEX,
                countries=countries or _search_country(profile),
                locales=_search_locales(profile),
            )
            results = expect_ok(ctx, response, f"search for '{query}'")

        products = [_shape_search_result(result) for result in results[:limit]]
        return round_floats(
            {
                "query": query,
                "returned": len(products),
                "total_matches": len(results),
                "products": products,
            }
        )

    @mcp.tool()
    async def get_product(ctx: Context, product_id: str) -> dict[str, Any]:
        """Look up one product's full detail, including its serving options.

        Use this before tracking when you need to know which serving units a
        product supports, or to get its complete nutrient breakdown rather than
        just the four macros that search returns.

        Args:
            product_id: The product's UUID, as returned by search_products.
        """
        async with yazio_client(ctx) as client:
            detail = await fetch_product(client, product_id)

        if detail is None:
            raise ToolError(f"no product found with id {product_id}")

        shaped = _shape_product(detail)
        # These values are per gram/ml and must survive multiplication by a
        # portion. Rounding 0.0248 g fat to 0.02 here loses 1.2 g in a 250 g drink.
        return {**round_floats(shaped),
                "nutrients_per_base_unit": shaped["nutrients_per_base_unit"]}

    @mcp.tool()
    async def get_suggested_products(
        ctx: Context,
        daytime: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """List the products YAZIO suggests for a given meal.

        These are drawn from what this user usually eats at that time of day, so
        they are the fastest route to logging a repeat meal: each suggestion
        already carries the amount and serving to track.

        Args:
            daytime: One of breakfast, lunch, dinner, snack.
            date: Day to get suggestions for as YYYY-MM-DD. Defaults to today.
        """
        slot = resolve_daytime(daytime)
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_list_suggested_products.asyncio_detailed(
                client=client, daytime=slot, date=day
            )
            suggestions = expect_ok(ctx, response, f"load {slot} suggestions")

            # Suggestions are bare ids; resolving them here is what makes the
            # result usable, since a caller cannot pick between UUIDs.
            details = [
                await fetch_product(client, _suggestion_id(item))
                for item in suggestions
            ]

        products = []
        for suggestion, detail in zip(suggestions, details, strict=True):
            entry = {
                "product_id": _suggestion_id(suggestion),
                "amount": plain(suggestion.amount),
                "serving": plain(suggestion.serving),
                "serving_quantity": plain(suggestion.serving_quantity),
            }
            if detail is not None:
                entry["name"] = detail.get("name")
                entry["producer"] = detail.get("producer")
            products.append(entry)

        return round_floats({"date": day, "daytime": slot, "products": products})


async def fetch_product(
    client: AuthenticatedClient, product_id: str
) -> dict[str, Any] | None:
    """Fetch one product as a plain dict.

    Returns None for a 404 so that callers resolving ids in bulk can tolerate
    one product having been deleted, rather than losing the whole diary day to
    a single dead reference.

    The requested id is folded into the result because the response body does
    not carry one — the caller already knows it, but everything downstream reads
    the product as a self-contained object.
    """
    try:
        response = await api_get_product.asyncio_detailed(
            client=client, id=product_id
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            f"could not reach YAZIO to load product {product_id}: {exc}"
        ) from exc

    if response.status_code == 404:
        return None
    if not 200 <= response.status_code < 300:
        raise ToolError(
            f"YAZIO returned {response.status_code} while loading product {product_id}"
        )
    if response.parsed is None:
        raise ToolError(f"YAZIO returned an unreadable body for product {product_id}")

    return {"id": product_id, **(plain(response.parsed) or {})}


def _search_country(profile: Any) -> str:
    """Pick the country whose food database the user actually eats from.

    YAZIO keeps this separate from the account country: someone living abroad
    may still want the database of their home country.
    """
    country = plain(profile.food_database_country) or plain(profile.country)
    return str(country or _FALLBACK_COUNTRY).upper()


def _search_locales(profile: Any) -> str:
    """Build the locale ranking hint from the user's language and country."""
    language = str(plain(profile.language) or "en").lower()
    country = _search_country(profile)
    return f"{language}_{country},en_{country}"


def _suggestion_id(suggestion: Any) -> str:
    product_id = plain(suggestion.product_id)
    if not isinstance(product_id, str):
        raise ToolError("YAZIO returned a suggestion without a product id")
    return product_id


def _shape_search_result(result: Any) -> dict[str, Any]:
    return {
        "product_id": plain(result.product_id),
        "name": plain(result.name),
        "producer": plain(result.producer),
        "is_verified": plain(result.is_verified),
        "base_unit": plain(result.base_unit),
        "serving": plain(result.serving),
        "serving_quantity": plain(result.serving_quantity),
        "amount": plain(result.amount),
        "nutrients_per_serving": group_nutrients(plain(result.nutrients) or {}),
        "language": plain(result.language),
        "countries": plain(result.countries),
    }


def _shape_product(detail: dict[str, Any]) -> dict[str, Any]:
    """Reshape a raw product payload, keeping anything we do not recognise.

    The spec models this endpoint loosely — the SDK keeps anything it does not
    name in `additional_properties`, which `plain` folds back in — so this pulls
    out the fields the app is known to use and passes the remainder through
    under `extra` rather than dropping whatever the spec has yet to catch up on.
    """
    known = {
        "id",
        "name",
        "producer",
        "base_unit",
        "is_verified",
        "is_private",
        "is_deleted",
        "has_ean",
        "nutrients",
        "servings",
        "eans",
        "category",
        "language",
        "countries",
        "updated_at",
    }

    shaped: dict[str, Any] = {
        "product_id": detail.get("id"),
        "name": detail.get("name"),
        "producer": detail.get("producer"),
        "base_unit": detail.get("base_unit"),
        "is_verified": detail.get("is_verified"),
        "category": detail.get("category"),
        "barcodes": detail.get("eans"),
        "servings": detail.get("servings"),
        # Per one base unit, not per 100: olive oil reads 8.84 kcal per gram.
        "nutrients_per_base_unit": group_nutrients(detail.get("nutrients") or {}),
    }

    extra = {key: value for key, value in detail.items() if key not in known}
    if extra:
        shaped["extra"] = extra

    return {key: value for key, value in shaped.items() if value is not None}

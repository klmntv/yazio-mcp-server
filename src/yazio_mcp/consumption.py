"""Persist the complete plan before POST and reconcile exact IDs afterwards."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.api.diary import add_consumed_items, list_consumed_items
from yazio_sdk.models import ConsumedItems

from .common import plain, resolve_date, resolve_timestamp
from .ledger import Ledger
from .session import EXPECTED_ACCOUNT, data_directory, expect_ok, operation_scope, yazio_client
from .state import state_lock


def matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isclose(expected, actual, rel_tol=1e-8, abs_tol=1e-8)
        )
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            matches(v, actual.get(k)) for k, v in expected.items()
        )
    return expected == actual


def verify_plan(plan: dict, diary: Any) -> list[dict]:
    result = []
    for item in plan["items"]:
        expected = item["entry"]
        entries = plain(getattr(diary, item["bucket"])) or []
        found = next(
            (
                row
                for row in entries
                if isinstance(row, dict)
                and str(row.get("id", "")).lower() == expected["id"].lower()
            ),
            None,
        )
        fields = {k: v for k, v in expected.items() if k not in {"id", "date"}}
        valid = (
            found is not None
            and matches(fields, found)
            and str(found.get("date", ""))[:10] == plan["date"]
        )
        result.append(
            {
                "entry_id": expected["id"],
                "bucket": item["bucket"],
                "state": "verified" if valid else ("changed" if found else "not_confirmed"),
            }
        )
    return result


async def record_consumption(
    ctx: Context,
    request_id: str,
    intent: dict,
    factory: Callable[[], Awaitable[list[dict]]],
    date: str | None,
    daytime: str,
) -> dict:
    scope = operation_scope(ctx)
    directory = data_directory(ctx)
    lock_key = hashlib.sha256(f"{scope}:{request_id}".encode()).hexdigest()
    async with state_lock(directory, f"operation-{lock_key}.lock"):
        binding = EXPECTED_ACCOUNT.set(scope)
        try:
            return await _record_consumption(ctx, request_id, intent, factory, date, daytime, scope)
        finally:
            EXPECTED_ACCOUNT.reset(binding)


async def _record_consumption(
    ctx: Context,
    request_id: str,
    intent: dict,
    factory: Callable[[], Awaitable[list[dict]]],
    date: str | None,
    daytime: str,
    scope: str,
) -> dict:
    ledger = Ledger(data_directory(ctx))
    previous = ledger.existing(scope, request_id, intent)
    owner = False
    if previous is None:
        items = await factory()
        if not items or len(items) > 100:
            raise ToolError("A meal operation needs 1 to 100 items")
        day = resolve_date(date)
        stamp = resolve_timestamp(date)
        plan = {
            "date": day,
            "items": [
                {
                    "bucket": item["bucket"],
                    "entry": {
                        **item["entry"],
                        "id": str(uuid.uuid4()).upper(),
                        "date": stamp,
                        "daytime": daytime,
                    },
                }
                for item in items
            ],
        }
        previous, owner = ledger.begin(scope, request_id, intent, plan)
    plan = json.loads(previous["plan"])
    if previous["state"] in {"committed", "failed"} and previous["result"]:
        return {**json.loads(previous["result"]), "replayed": True}

    status_code = None
    checked = None
    async with yazio_client(ctx) as client:
        if owner:
            payload = {"products": [], "recipe_portions": [], "simple_products": []}
            for item in plan["items"]:
                payload[item["bucket"]].append(item["entry"])
            try:
                response = await add_consumed_items.asyncio_detailed(
                    client=client,
                    body=ConsumedItems.from_dict(payload),
                )
                status_code = response.status_code
            except httpx.HTTPError:
                pass  # The server may have saved it. Never replay this POST blindly.
        try:
            response = await list_consumed_items.asyncio_detailed(client=client, date=plan["date"])
            diary = expect_ok(ctx, response, "verify the recorded meal")
            checked = verify_plan(plan, diary)
        except (httpx.HTTPError, ToolError, ValueError, TypeError, KeyError):
            # A verification failure is an unknown result, not permission to resend.
            checked = None

    count = sum(item["state"] == "verified" for item in checked or [])
    if checked is not None and count == len(plan["items"]):
        status = "committed"
    elif checked is not None and any(item["state"] != "not_confirmed" for item in checked):
        status = "partial"
    elif status_code is not None and 400 <= status_code < 500:
        status = "failed"
    else:
        status = "uncertain"
    result = {
        "request_id": request_id,
        "status": status,
        "date": plan["date"],
        "tracked": True if status == "committed" else (False if status == "failed" else None),
        "verified": status == "committed",
        "replayed": not owner,
        "items": checked
        or [
            {
                "entry_id": item["entry"]["id"],
                "bucket": item["bucket"],
                "state": "not_confirmed",
            }
            for item in plan["items"]
        ],
    }
    ledger.finish(scope, request_id, result)
    return result

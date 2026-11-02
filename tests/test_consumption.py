"""Lost acknowledgements, restarts and separate identical meals must stay distinct."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.models import ConsumedItems

from yazio_mcp import common, consumption
from yazio_mcp.consumption import record_consumption
from yazio_mcp.ledger import Ledger


@pytest.fixture
def backend(tmp_path, monkeypatch):
    state = SimpleNamespace(
        rows={"products": [], "recipe_portions": [], "simple_products": []},
        posts=[],
        lose_response=False,
        discard=False,
        partial=False,
        read_fails=False,
        delay=0,
    )

    @asynccontextmanager
    async def client(_ctx):
        yield object()

    async def write(*, client, body):
        payload = body.to_dict()
        # The operation's exact remote IDs must already survive a crash here.
        with Ledger(tmp_path).connection() as db:
            plan = json.loads(
                db.execute("SELECT plan FROM operations ORDER BY updated_at DESC").fetchone()[0]
            )
        assert {x["entry"]["id"] for x in plan["items"]} == {
            row["id"] for bucket in payload.values() for row in bucket
        }
        state.posts.append(copy.deepcopy(payload))
        if state.delay:
            await asyncio.sleep(state.delay)
        if not state.discard:
            for bucket, rows in payload.items():
                state.rows[bucket].extend(copy.deepcopy(rows[:1] if state.partial else rows))
        if state.lose_response:
            raise httpx.ReadTimeout("acknowledgement was lost")
        return SimpleNamespace(status_code=204)

    async def read(*, client, date):
        if state.read_fails:
            raise httpx.ReadTimeout("read failed")
        return SimpleNamespace(
            status_code=200,
            content=b"",
            parsed=ConsumedItems.from_dict(state.rows),
        )

    monkeypatch.setattr(consumption, "yazio_client", client)
    monkeypatch.setattr(consumption, "data_directory", lambda _ctx: tmp_path)
    monkeypatch.setattr(consumption, "operation_scope", lambda _ctx: "account-a")
    monkeypatch.setattr(consumption.add_consumed_items, "asyncio_detailed", write)
    monkeypatch.setattr(consumption.list_consumed_items, "asyncio_detailed", read)
    return state


async def factory():
    return [{"bucket": "products", "entry": {"product_id": "yogurt", "amount": 100.0}}]


async def record(request_id, factory=factory, date="2026-11-02", **intent):
    return await record_consumption(
        object(),
        request_id,
        {"food": "yogurt", "date": date, **intent},
        factory,
        date,
        "snack",
    )


@pytest.mark.asyncio
async def test_lost_post_response_is_verified_without_a_second_write(backend):
    backend.lose_response = True
    result = await record("message-1")
    assert result["status"] == "committed"
    assert result["verified"] is True
    assert (await record("message-1"))["replayed"] is True
    assert len(backend.posts) == 1


@pytest.mark.asyncio
async def test_two_new_commands_can_record_identical_portions(backend):
    assert (await record("first-portion"))["status"] == "committed"
    assert (await record("second-portion"))["status"] == "committed"
    assert len(backend.rows["products"]) == 2
    assert backend.rows["products"][0]["id"] != backend.rows["products"][1]["id"]


@pytest.mark.asyncio
async def test_acknowledged_but_absent_food_is_uncertain_and_not_reposted(backend):
    backend.discard = True
    result = await record("missing-food")
    assert result["status"] == "uncertain"
    assert result["tracked"] is None
    assert (await record("missing-food"))["status"] == "uncertain"
    assert len(backend.posts) == 1


@pytest.mark.asyncio
async def test_restart_reconciles_the_saved_ids_and_original_day(backend, monkeypatch):
    backend.discard = True
    monkeypatch.setattr(
        common, "local_now", lambda: datetime(2026, 11, 2, 23, 59, tzinfo=ZoneInfo("Europe/Moscow"))
    )
    assert (await record("midnight", date=None))["status"] == "uncertain"
    backend.rows = copy.deepcopy(backend.posts[0])
    monkeypatch.setattr(
        common, "local_now", lambda: datetime(2026, 11, 3, 0, 1, tzinfo=ZoneInfo("Europe/Moscow"))
    )

    async def do_not_replan():
        raise AssertionError("The saved operation must not be replanned after restart")

    result = await record("midnight", factory=do_not_replan, date=None)
    assert result["status"] == "committed"
    assert result["date"] == "2026-11-02"
    assert len(backend.posts) == 1


@pytest.mark.asyncio
async def test_partial_bulk_write_reports_exact_items_without_retrying(backend):
    backend.partial = True

    async def two_foods():
        return (await factory()) + [
            {"bucket": "products", "entry": {"product_id": "rice", "amount": 150}}
        ]

    result = await record("partial", factory=two_foods)
    assert result["status"] == "partial"
    assert [item["state"] for item in result["items"]] == ["verified", "not_confirmed"]
    assert (await record("partial", factory=two_foods))["status"] == "partial"
    assert len(backend.posts) == 1


@pytest.mark.asyncio
async def test_reusing_a_request_id_with_changed_arguments_is_refused(backend):
    await record("one-action")
    with pytest.raises(ToolError, match="different parameters"):
        await record("one-action", food="something-else")
    assert len(backend.posts) == 1


@pytest.mark.asyncio
async def test_a_read_failure_after_success_does_not_claim_committed(backend):
    backend.read_fails = True
    assert (await record("read-error"))["status"] == "uncertain"
    backend.read_fails = False
    assert (await record("read-error"))["status"] == "committed"
    assert len(backend.posts) == 1


@pytest.mark.asyncio
async def test_same_id_with_a_changed_quantity_is_not_verified_or_overwritten(backend):
    backend.discard = True
    await record("manual-change")
    backend.rows = copy.deepcopy(backend.posts[0])
    backend.rows["products"][0]["amount"] = 75
    result = await record("manual-change")
    assert result["status"] == "partial"
    assert result["items"][0]["state"] == "changed"
    assert backend.rows["products"][0]["amount"] == 75


def test_other_accounts_cannot_see_the_operation_journal(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.begin("one-account", "action", {"a": 1}, {"items": []})
    with pytest.raises(ToolError, match="belongs to this account"):
        ledger.get_operation("another-account", "action")


@pytest.mark.asyncio
async def test_concurrent_delivery_has_only_one_post(backend):
    backend.delay = 0.02
    results = await asyncio.gather(record("same-delivery"), record("same-delivery"))
    assert any(result["status"] == "committed" for result in results)
    assert len(backend.posts) == 1
    assert len(backend.rows["products"]) == 1

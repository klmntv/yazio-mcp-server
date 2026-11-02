"""A beverage's amount must retain the catalog unit instead of implying ml."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yazio_mcp.server import build_server
from yazio_mcp.tools import diary


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", ["g", "ml"])
async def test_diary_preserves_the_product_card_unit_for_a_drink(monkeypatch, unit):
    @asynccontextmanager
    async def client(_ctx):
        yield object()

    async def response(**kwargs):
        return object()

    async def product(_client, _id):
        return {"name": "Cappuccino", "producer": "Cafe", "base_unit": unit}

    items = SimpleNamespace(
        products=[{"id": "entry", "product_id": "coffee", "daytime": "breakfast",
                   "amount": 250, "serving": "glass", "serving_quantity": 1}],
        recipe_portions=[], simple_products=[],
    )
    monkeypatch.setattr(diary, "yazio_client", client)
    monkeypatch.setattr(diary.api_list_consumed_items, "asyncio_detailed", response)
    monkeypatch.setattr(diary, "expect_ok", lambda *args: items)
    monkeypatch.setattr(diary, "fetch_product", product)
    _, result = await build_server().call_tool("get_diary", {"date": "2026-11-02"})
    entry = result["meals"]["breakfast"][0]
    assert entry["amount"] == 250
    assert entry["base_unit"] == unit

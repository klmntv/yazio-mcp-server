from contextlib import asynccontextmanager

import pytest

from yazio_mcp.server import build_server
from yazio_mcp.tools import products


@pytest.mark.asyncio
async def test_per_base_unit_nutrients_are_not_rounded_before_portion_scaling(monkeypatch):
    @asynccontextmanager
    async def client(_ctx):
        yield object()

    async def product(_client, _id):
        return {"id": "drink", "name": "Coffee", "base_unit": "g", "nutrients": {
            "energy.energy": .45678, "nutrient.fat": .0248, "nutrient.protein": .123456,
        }}

    monkeypatch.setattr(products, "yazio_client", client)
    monkeypatch.setattr(products, "fetch_product", product)
    _, result = await build_server().call_tool("get_product", {"product_id": "drink"})
    nutrients = result["nutrients_per_base_unit"]
    assert nutrients["energy_kcal"] == .45678
    assert nutrients["macros"]["fat"] * 250 == pytest.approx(6.2)
    assert nutrients["macros"]["protein"] == .123456

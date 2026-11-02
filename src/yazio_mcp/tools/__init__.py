"""Tool implementations, grouped the way the YAZIO app groups its features.

Each module exposes a `register(mcp)` function so that server.py stays a list of
registrations rather than a thousand-line file of tool bodies.

Endpoint modules from `yazio_sdk` are imported under an `api_` prefix throughout
this package. Several of them share a name with a tool defined here — the SDK's
`get_product`, `search_products`, `get_goals`, `get_recipe` and `log_exercise`
all do — and because the tools are nested inside `register`, an unprefixed
import would be shadowed by the tool's own closure rather than clashing loudly.
The prefix keeps the two apart, and says at every call site which is which.
"""

from mcp.server.fastmcp import FastMCP

from . import body, diary, meals, products, recipes, tracking


def register_all(mcp: FastMCP) -> None:
    """Attach every tool in this package to the server."""
    diary.register(mcp)
    products.register(mcp)
    tracking.register(mcp)
    recipes.register(mcp)
    body.register(mcp)
    meals.register(mcp)

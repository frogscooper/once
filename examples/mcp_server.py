"""An MCP server whose charge tool runs at most once per order.

The model passes an order id; that is the scope, so a retried or replayed
call for the same order returns the stored charge instead of charging again.

    pip install git+https://github.com/frogscooper/once mcp
    python examples/mcp_server.py          # serve over stdio
    python examples/mcp_server.py --demo   # call the tool twice in-process
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

try:  # mcp 2.x
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context  # type: ignore[no-redef,attr-defined,unused-ignore]
    from mcp.server.fastmcp import FastMCP as Server  # type: ignore[no-redef,attr-defined,unused-ignore]

from once import Once
from once.integrations.mcp import once_tool

once = Once("mcp_example.db")
server = Server("payments")
charges: list[dict[str, Any]] = []


@server.tool()
@once_tool(once, name="charge_card", scope_arg="order_id")
async def charge_card(
    order_id: str, amount: int, customer: str, ctx: Context, idempotency_key: str | None = None
) -> dict[str, str]:
    """Charge a customer for an order."""
    # pass idempotency_key on to the payment API as well
    charges.append({"order": order_id, "amount": amount, "key": idempotency_key})
    return {"charge_id": f"ch_{len(charges)}"}


async def demo() -> None:
    args = {"order_id": "order_456", "amount": 1000, "customer": "cus_123"}
    for _ in range(2):
        result = await server.call_tool("charge_card", args)
        print(result)
    print("charges made:", len(charges))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        asyncio.run(demo())
    else:
        server.run()

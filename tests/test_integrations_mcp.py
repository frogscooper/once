"""Run the MCP adapter against the real SDK. Skipped if mcp is not installed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

try:  # mcp 2.x
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context  # type: ignore[no-redef,attr-defined,unused-ignore]
    from mcp.server.fastmcp import FastMCP as Server  # type: ignore[no-redef,attr-defined,unused-ignore]

from once import Once
from once.integrations.mcp import once_tool


def schema(tool: Any) -> dict[str, Any]:
    value = getattr(tool, "input_schema", None) or tool.inputSchema
    assert isinstance(value, dict)
    return value


def test_server_tool_runs_once(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    server = Server("test")
    calls: list[str | None] = []

    @server.tool()
    @once_tool(once, name="charge_card", scope_arg="order_id")
    async def charge_card(
        order_id: str, amount: int, ctx: Context, idempotency_key: str | None = None
    ) -> str:
        """Charge."""
        calls.append(idempotency_key)
        return f"ch_{len(calls)}"

    async def main() -> list[Any]:
        tools = await server.list_tools()
        props = schema(next(t for t in tools if t.name == "charge_card"))["properties"]
        assert set(props) == {"order_id", "amount"}
        args = {"order_id": "o1", "amount": 5}
        return [
            await server.call_tool("charge_card", args),
            await server.call_tool("charge_card", args),
            await server.call_tool("charge_card", {"order_id": "o2", "amount": 5}),
        ]

    results = asyncio.run(main())
    assert len(calls) == 2
    assert calls[0] is not None and len(calls[0]) == 64
    assert "ch_1" in str(results[0]) and "ch_1" in str(results[1]) and "ch_2" in str(results[2])


def test_server_tool_with_key(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    server = Server("test")
    calls: list[int] = []
    seen: list[dict[str, Any]] = []

    def pick(args: dict[str, Any]) -> str:
        seen.append(args)
        return str(args["order_id"])

    @server.tool()
    @once_tool(once, name="refund", key=pick)
    async def refund(order_id: str, amount: int, ctx: Context) -> str:
        """Refund."""
        calls.append(amount)
        return f"re_{len(calls)}"

    async def main() -> list[Any]:
        return [
            await server.call_tool("refund", {"order_id": "o1", "amount": 5}),
            await server.call_tool("refund", {"order_id": "o1", "amount": 4}),
        ]

    results = asyncio.run(main())
    assert calls == [5]
    assert "re_1" in str(results[0]) and "re_1" in str(results[1])
    assert set(seen[0]) == {"order_id", "amount"}

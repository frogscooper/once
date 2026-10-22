"""Run the CrewAI adapter against crewai. Skipped if missing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("crewai")

from crewai.tools import BaseTool, tool

from once import Once
from once.integrations.crewai import once_tool


def test_tool_run_and_arun(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    charges: list[str | None] = []

    @tool("charge")
    @once_tool(once, name="charge", scope_arg="order_id")
    def charge(order_id: str, amount: int, idempotency_key: str = "") -> str:
        """Charge a card for an order."""
        charges.append(idempotency_key)
        return f"ch_{len(charges)}"

    assert set(charge.args_schema.model_json_schema()["properties"]) == {"order_id", "amount"}
    assert charge.run(order_id="o1", amount=5) == "ch_1"
    assert charge.run(order_id="o1", amount=5) == "ch_1"
    assert charge.run(order_id="o2", amount=5) == "ch_2"

    @tool("notify")
    @once_tool(once, name="notify")
    async def notify(to: str) -> str:
        """Send a notification."""
        charges.append(to)
        return "sent"

    async def main() -> list[Any]:
        with once.scope("run_1"):
            return [await notify.arun(to="a"), await notify.arun(to="a")]

    assert asyncio.run(main()) == ["sent", "sent"]
    assert charges[-1] == "a"
    assert len(charges) == 3


def test_base_tool_subclass(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    calls: list[int] = []

    class Refund(BaseTool):
        name: str = "refund"
        description: str = "Refund an order."

        @once_tool(once, name="refund")
        def _run(self, order_id: str) -> str:
            calls.append(1)
            return f"re_{order_id}"

    assert set(Refund().args_schema.model_json_schema()["properties"]) == {"order_id"}
    assert Refund().run(order_id="o1") == "re_o1"
    assert Refund().run(order_id="o1") == "re_o1"
    assert len(calls) == 1

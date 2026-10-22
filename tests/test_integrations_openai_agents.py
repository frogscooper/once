"""Run the OpenAI Agents SDK adapter against openai-agents. Skipped if missing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("agents")
pytest.importorskip("agents.testing")

from agents import Agent, RunConfig, Runner, function_tool
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext

from once import Once
from once.integrations.openai_agents import once_tool


def test_runner_dedupes_per_group(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    charges: list[str | None] = []

    @function_tool
    @once_tool(once, name="charge")
    async def charge(ctx: ToolContext[Any], amount: int, idempotency_key: str | None = None) -> str:
        """Charge a card."""
        charges.append(ctx.tool_call_id)
        return f"ch_{len(charges)}"

    assert set(charge.params_json_schema["properties"]) == {"amount"}

    async def run(group: str | None, call_id: str) -> Any:
        model = ScriptedModel(
            [[function_call("charge", {"amount": 5}, call_id=call_id)], [assistant_message("ok")]]
        )
        agent = Agent(name="billing", model=model, tools=[charge])
        config = RunConfig(group_id=group, tracing_disabled=True)
        return (await Runner.run(agent, "go", run_config=config)).final_output

    async def main() -> None:
        await run("conv_1", "c1")
        await run("conv_1", "c2")  # retried with a new call id: no second charge
        await run("conv_2", "c3")
        with once.scope("job_1"):
            await run(None, "c4")
            await run(None, "c5")

    asyncio.run(main())
    assert charges == ["c1", "c3", "c4"]

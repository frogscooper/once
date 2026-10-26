"""An OpenAI Agents SDK agent that charges a card at most once per conversation.

No API key needed: a scripted model asks for the same tool call every run, the
way a real model does when a turn is retried. The conversation id goes in
``RunConfig.group_id``.

    pip install agent-once openai-agents
    python examples/openai_agents_example.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from agents import Agent, RunConfig, Runner, function_tool
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext

from once import Once
from once.integrations.openai_agents import once_tool

once = Once(os.path.join(tempfile.mkdtemp(), "once.db"))
charges: list[int] = []


@function_tool
@once_tool(once, name="charge_card")
async def charge_card(ctx: ToolContext[Any], customer: str, amount: int) -> str:
    """Charge a customer's card."""
    charges.append(amount)
    return f"ch_{len(charges)}"


async def main() -> None:
    for i, conversation in enumerate(["conv_1", "conv_1", "conv_2"]):
        args = {"customer": "cus_1", "amount": 500}
        call = function_call("charge_card", args, call_id=f"call_{i}")
        model = ScriptedModel([[call], [assistant_message("charged")]])
        agent = Agent(name="billing", model=model, tools=[charge_card])
        config = RunConfig(group_id=conversation, tracing_disabled=True)
        result = await Runner.run(agent, "charge cus_1 $5", run_config=config)
        print(conversation, result.final_output)
    print("charges made:", len(charges))  # 2: one per conversation


if __name__ == "__main__":
    asyncio.run(main())

"""Run the Pydantic AI adapter against pydantic-ai. Skipped if missing."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from once import Once
from once.integrations.pydantic_ai import once_tool


def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(parts=[ToolCallPart("charge", {"amount": 5})])
    return ModelResponse(parts=[TextPart("done")])


def test_agent_scopes(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    charges: list[str | None] = []

    async def charge(ctx: RunContext[None], amount: int, idempotency_key: str = "") -> str:
        """Charge a card."""
        charges.append(idempotency_key)
        return f"ch_{len(charges)}"

    agent: Agent[None, str] = Agent(FunctionModel(model), tools=[once_tool(once, charge)])
    agent.run_sync("go", conversation_id="conv_1")
    agent.run_sync("go", conversation_id="conv_1")
    agent.run_sync("go", conversation_id="conv_2")
    assert len(charges) == 2
    with once.scope("job_1"):  # the enclosing scope comes before the conversation id
        agent.run_sync("go", conversation_id="conv_3")
        agent.run_sync("go", conversation_id="conv_4")
    assert len(charges) == 3
    agent.run_sync("go")  # Pydantic AI makes up a conversation id for each run
    agent.run_sync("go")
    assert len(charges) == 5
    assert len(set(charges)) == 5


def test_tool_schema_hides_context_and_key(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    seen: list[dict[str, object]] = []

    def spy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.parameters_json_schema for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[None, str] = Agent(FunctionModel(spy))

    @agent.tool
    @once_tool(once)
    def refund(ctx: RunContext[None], order_id: str, idempotency_key: str = "") -> str:
        """Refund an order."""
        return order_id

    agent.run_sync("go")
    assert set(seen[0]["properties"]) == {"order_id"}  # type: ignore[call-overload]

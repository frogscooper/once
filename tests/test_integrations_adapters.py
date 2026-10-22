"""OpenAI Agents, Pydantic AI and CrewAI adapter tests that need no framework."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from once import Once, make_key
from once.integrations.crewai import once_tool as crew_tool
from once.integrations.openai_agents import group_id
from once.integrations.openai_agents import once_tool as oa_tool
from once.integrations.pydantic_ai import context_scope
from once.integrations.pydantic_ai import once_tool as pa_tool


@pytest.fixture
def once(tmp_path: Path) -> Once:
    return Once(tmp_path / "once.db")


class ToolContext:
    """Stands in for the Agents SDK ToolContext: not JSON serializable."""

    def __init__(self, group: str | None = None, user: str | None = None) -> None:
        self.run_config = SimpleNamespace(group_id=group)
        self.context = SimpleNamespace(user=user)


class RunContext:
    """Stands in for Pydantic AI's RunContext."""

    def __init__(self, conversation: str | None = None, run: str | None = None) -> None:
        self.conversation_id = conversation
        self.run_id = run
        self.deps = SimpleNamespace(user=None)


def test_group_id_variants() -> None:
    assert group_id(None) is None
    assert group_id(ToolContext()) is None
    assert group_id(ToolContext("g1")) == "g1"
    assert group_id(SimpleNamespace(run_config={"group_id": "g2"})) == "g2"
    assert group_id(SimpleNamespace(run_config={})) is None


def test_openai_tool_scopes_by_group(once: Once) -> None:
    calls = 0

    @oa_tool(once, name="charge")
    async def charge(ctx: ToolContext, amount: int, idempotency_key: str | None = None) -> str:
        nonlocal calls
        calls += 1
        assert idempotency_key == make_key("charge", {"amount": amount}, group_id(ctx))
        return f"ch_{calls}"

    assert list(inspect.signature(charge).parameters) == ["ctx", "amount"]

    async def main() -> list[str]:
        return [
            await charge(ToolContext("g1"), 1),
            await charge(ToolContext("g1"), 1),
            await charge(ToolContext("g2"), 1),
            await charge(ToolContext(), 1),
        ]

    assert asyncio.run(main()) == ["ch_1", "ch_1", "ch_2", "ch_3"]


def test_openai_tool_scope_arg_and_scope_from(once: Once) -> None:
    calls = 0

    @oa_tool(once, scope_arg="order_id", scope_from=lambda ctx: ctx.context.user)
    def refund(wrapper: Any, order_id: str | None, amount: int) -> int:
        nonlocal calls
        calls += 1
        return calls

    assert refund(ToolContext("g1"), "o1", 5) == 1
    assert refund(ToolContext("g2"), "o1", 5) == 1  # scope is the order
    assert refund(ToolContext("g1", user="u1"), None, 5) == 2  # then scope_from
    assert refund(ToolContext("g2", user="u1"), None, 5) == 2
    assert refund(ToolContext("g1"), None, 5) == 3  # scope_from gave None: group id
    assert refund(ToolContext("g1"), None, 5) == 3


def test_openai_tool_without_context(once: Once) -> None:
    @oa_tool(once)
    def ping(n: int) -> int:
        return n

    with once.scope("outer"):
        assert ping(1) == 1
    assert once.get(make_key("ping", {"n": 1}, "outer")) is not None

    def pong(n: int) -> int:
        return n

    assert oa_tool(once, pong)(2) == 2


def test_context_scope() -> None:
    ctx = RunContext("c1", "r1")
    assert context_scope(ctx, "conversation") == "c1"
    assert context_scope(ctx, "run") == "r1"
    assert context_scope(RunContext(), "run") is None
    assert context_scope(object(), "conversation") is None


def test_pydantic_tool_enclosing_scope_by_default(once: Once) -> None:
    calls = 0

    @pa_tool(once, name="send")
    def send(ctx: RunContext, to: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    assert list(inspect.signature(send).parameters) == ["ctx", "to"]
    # a fresh conversation id per run does not change the key
    assert send(RunContext("c1"), "a") == 1
    assert send(RunContext("c2"), "a") == 1
    with once.scope("job_1"):
        assert send(RunContext("c3"), "a") == 2
        assert send(RunContext("c4"), "a") == 2


def test_pydantic_tool_opt_in_scopes(once: Once) -> None:
    calls = 0

    @pa_tool(once, scope="conversation")
    async def by_conv(ctx: RunContext, to: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    @pa_tool(once, scope="run", context_arg="rc", scope_arg="order_id")
    def by_run(rc: Any, order_id: str | None) -> int:
        nonlocal calls
        calls += 1
        return calls

    async def main() -> list[int]:
        return [
            await by_conv(RunContext("c1"), "a"),
            await by_conv(RunContext("c1"), "a"),
            await by_conv(RunContext("c2"), "a"),
        ]

    assert asyncio.run(main()) == [1, 1, 2]
    assert by_run(RunContext(run="r1"), None) == 3
    assert by_run(RunContext(run="r1"), None) == 3
    assert by_run(RunContext(run="r2"), "o1") == 4
    assert by_run(RunContext(run="r3"), "o1") == 4


def test_pydantic_tool_scope_from(once: Once) -> None:
    @pa_tool(once, scope_from=lambda ctx: ctx.deps.user, scope="run")
    def ping(ctx: RunContext, n: int) -> str:
        return f"{n}"

    ctx = RunContext(run="r1")
    ctx.deps.user = "u1"
    assert ping(ctx, 1) == "1"
    assert once.get(make_key("ping", {"n": 1}, "u1")) is not None
    assert ping(RunContext(run="r1"), 1) == "1"  # scope_from gave None: run id
    assert once.get(make_key("ping", {"n": 1}, "r1")) is not None


def test_pydantic_tool_without_context_and_bad_scope(once: Once) -> None:
    def add(a: int, b: int) -> int:
        return a + b

    assert pa_tool(once, add)(1, 2) == 3
    with pytest.raises(ValueError, match="scope must be"):
        pa_tool(once, scope="thread")  # type: ignore[call-overload]


def test_crewai_tool_scopes(once: Once) -> None:
    calls = 0

    @crew_tool(once, name="charge", scope_arg="order_id")
    def charge(order_id: str | None, amount: int) -> int:
        nonlocal calls
        calls += 1
        return calls

    assert charge("o1", 5) == 1
    assert charge("o1", 5) == 1
    assert charge(None, 5) == 2
    with once.scope("run_1"):
        assert charge(None, 5) == 3
    assert once.get(make_key("charge", {"order_id": None, "amount": 5}, "run_1")) is not None
    assert crew_tool(once, lambda n: n)(4) == 4


def test_crewai_method_leaves_self_out(once: Once) -> None:
    class ChargeTool:
        def __init__(self) -> None:
            self.calls = 0

        @crew_tool(once, name="charge")
        def _run(self, amount: int) -> int:
            self.calls += 1
            return self.calls

    assert ChargeTool()._run(5) == 1
    assert ChargeTool()._run(5) == 1  # another instance, same key

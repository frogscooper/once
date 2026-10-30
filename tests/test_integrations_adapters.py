"""OpenAI Agents, Pydantic AI and CrewAI adapter tests that need no framework."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from once import Once, make_key
from once.integrations import langchain, mcp
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
    async def charge(ctx: ToolContext, amount: int, idempotency_key: str = "") -> str:
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


def test_pydantic_tool_default_scope_order(once: Once) -> None:
    calls = 0

    @pa_tool(once, name="send")
    def send(ctx: RunContext, to: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    assert list(inspect.signature(send).parameters) == ["ctx", "to"]
    # conversation id by default
    assert send(RunContext("c1"), "a") == 1
    assert send(RunContext("c1"), "a") == 1
    assert send(RunContext("c2"), "a") == 2
    assert once.get(make_key("send", {"to": "a"}, "c2")) is not None
    # the enclosing scope comes before the conversation id
    with once.scope("job_1"):
        assert send(RunContext("c3"), "a") == 3
        assert send(RunContext("c4"), "a") == 3
    assert once.get(make_key("send", {"to": "a"}, "job_1")) is not None
    # no conversation id and no enclosing scope: unscoped key
    assert send(RunContext(), "a") == 4
    assert once.get(make_key("send", {"to": "a"}, None)) is not None


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
    with once.scope("job_1"):
        assert ping(ctx, 2) == "2"  # scope_from beats the enclosing scope
        assert ping(RunContext(run="r1"), 3) == "3"  # enclosing beats the run id
    assert once.get(make_key("ping", {"n": 2}, "u1")) is not None
    assert once.get(make_key("ping", {"n": 3}, "job_1")) is not None


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


ADAPTERS: dict[str, Callable[..., Any]] = {
    "mcp": mcp.once_tool,
    "langchain": langchain.once_tool,
    "openai_agents": oa_tool,
    "pydantic_ai": pa_tool,
    "crewai": crew_tool,
}


def test_ttl_reaches_the_record_and_expires(once: Once) -> None:
    calls: dict[str, int] = dict.fromkeys(ADAPTERS, 0)
    tools: dict[str, Callable[..., int]] = {}
    for label, adapter in ADAPTERS.items():

        def run(n: int, label: str = label) -> int:
            calls[label] += 1
            return calls[label]

        tools[label] = adapter(once, name=label, ttl=0.25)(run)

    for label, tool in tools.items():
        assert tool(1) == 1
        assert tool(1) == 1
        record = once.get(make_key(label, {"n": 1, "label": label}, None))
        assert record is not None and record.expires_at is not None
    time.sleep(0.4)
    for tool in tools.values():
        assert tool(1) == 2  # expired, so it runs again


def test_ttl_timedelta_and_no_ttl(once: Once) -> None:
    @oa_tool(once, ttl=timedelta(hours=1))
    def kept(n: int) -> int:
        return n

    @pa_tool(once)
    def forever(n: int) -> int:
        return n

    kept(1)
    forever(1)
    kept_record = once.get(make_key("kept", {"n": 1}, None))
    forever_record = once.get(make_key("forever", {"n": 1}, None))
    assert kept_record is not None and kept_record.expires_at is not None
    assert forever_record is not None and forever_record.expires_at is None


def test_key_reaches_every_adapter(once: Once) -> None:
    calls: dict[str, int] = dict.fromkeys(ADAPTERS, 0)
    seen: dict[str, dict[str, Any]] = {}
    tools: dict[str, Callable[..., int]] = {}
    for label, adapter in ADAPTERS.items():

        def pick(args: dict[str, Any], label: str = label) -> str:
            seen[label] = args
            return str(args["order_id"])

        def run(order_id: str, amount: int, label: str = label) -> int:
            calls[label] += 1
            return amount

        tools[label] = adapter(once, name=label, key=pick)(run)

    for label, tool in tools.items():
        assert tool("o1", 5) == 5
        assert tool("o1", 4) == 5  # same order, re-planned amount: replayed
        assert tool("o2", 4) == 4
        assert calls[label] == 2
        assert once.get(make_key(label, {"key": "o1"}, None)) is not None


def test_key_does_not_see_the_context(once: Once) -> None:
    seen: list[dict[str, Any]] = []

    def pick(args: dict[str, Any]) -> str:
        seen.append(args)
        return str(args["order_id"])

    @oa_tool(once, name="refund", key=pick)
    async def refund(ctx: ToolContext, order_id: str, amount: int) -> int:
        return amount

    @pa_tool(once, name="refund", key=pick)
    async def refund2(ctx: RunContext, order_id: str, amount: int) -> int:
        return amount

    class RefundTool:
        @crew_tool(once, name="refund3", key=pick)
        def _run(self, order_id: str, amount: int) -> int:
            return amount

    async def main() -> list[int]:
        return [
            await refund(ToolContext("g1"), "o1", 5),
            await refund(ToolContext("g1"), "o1", 6),
            await refund2(RunContext("c1"), "o1", 7),
        ]

    assert asyncio.run(main()) == [5, 5, 7]
    assert RefundTool()._run("o1", 8) == 8
    assert all(set(args) == {"order_id", "amount"} for args in seen)

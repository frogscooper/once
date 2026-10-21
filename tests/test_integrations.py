"""Adapter tests that need no framework installed."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from once import Once, make_key
from once.integrations.langchain import config_scope
from once.integrations.langchain import once_tool as lc_tool
from once.integrations.mcp import once_tool as mcp_tool
from once.integrations.mcp import session_id


@pytest.fixture
def once(tmp_path: Path) -> Once:
    return Once(tmp_path / "once.db")


class Context:
    """Stands in for mcp's Context: not JSON serializable, has headers."""

    def __init__(self, session: str | None) -> None:
        self.headers = {"mcp-session-id": session} if session else {}


def test_session_id_variants() -> None:
    assert session_id(None) is None
    assert session_id(Context("s1")) == "s1"
    assert session_id(Context(None)) is None
    # mcp 1.x: ctx.request_context.request.headers
    v1 = SimpleNamespace(
        headers=None,
        request_context=SimpleNamespace(request=SimpleNamespace(headers={"mcp-session-id": "s2"})),
    )
    assert session_id(v1) == "s2"
    no_request = SimpleNamespace(request_context=SimpleNamespace(request=None))
    assert session_id(no_request) is None

    class Outside:
        @property
        def headers(self) -> Any:
            raise ValueError("no active request")

    assert session_id(Outside()) is None


def test_mcp_tool_scopes_by_session(once: Once) -> None:
    calls = 0

    @mcp_tool(once, name="charge")
    async def charge(amount: int, ctx: Context, idempotency_key: str | None = None) -> str:
        nonlocal calls
        calls += 1
        assert idempotency_key == make_key(
            "charge", {"amount": amount}, ctx.headers.get("mcp-session-id")
        )
        return f"ch_{calls}"

    # the framework sees no idempotency_key parameter
    assert list(inspect.signature(charge).parameters) == ["amount", "ctx"]

    async def main() -> list[str]:
        return [
            await charge(1, Context("s1")),
            await charge(1, Context("s1")),
            await charge(1, Context("s2")),
            await charge(1, Context(None)),
        ]

    assert asyncio.run(main()) == ["ch_1", "ch_1", "ch_2", "ch_3"]


def test_mcp_tool_scope_arg_and_sync(once: Once) -> None:
    calls = 0

    @mcp_tool(once, scope_arg="order_id")
    def refund(order_id: str, amount: int, context: Any) -> int:
        nonlocal calls
        calls += 1
        return calls

    assert refund("o1", 5, Context("s1")) == 1
    assert refund("o1", 5, Context("s2")) == 1  # scope is the order, not the session
    assert refund("o2", 5, Context("s1")) == 2


def test_mcp_tool_without_context(once: Once) -> None:
    @mcp_tool(once)
    def ping(n: int) -> int:
        return n

    with once.scope("outer"):
        assert ping(1) == 1
    assert once.get(make_key("ping", {"n": 1}, "outer")) is not None


def test_config_scope() -> None:
    assert config_scope(None) is None
    assert config_scope({}) is None
    assert config_scope({"configurable": None}) is None
    assert config_scope({"configurable": {"thread_id": 7}}) == "7"
    assert config_scope({"configurable": {"run": "r"}}, "run") == "r"


def test_langchain_tool_scopes_by_thread(once: Once) -> None:
    calls = 0

    @lc_tool(once)
    def send(to: str, config: dict[str, Any]) -> int:
        nonlocal calls
        calls += 1
        return calls

    t1 = {"configurable": {"thread_id": "t1"}}
    assert send("a", t1) == 1
    assert send("a", {"configurable": {"thread_id": "t1"}, "callbacks": [object()]}) == 1
    assert send("a", {"configurable": {"thread_id": "t2"}}) == 2


def test_langchain_tool_async_and_custom_names(once: Once) -> None:
    calls = 0

    @lc_tool(once, scope_key="run", config_arg="cfg", name="post")
    async def post(text: str, cfg: dict[str, Any]) -> int:
        nonlocal calls
        calls += 1
        return calls

    async def main() -> list[int]:
        run = {"configurable": {"run": "r1"}}
        return [await post("hi", run), await post("hi", run), await post("hi", {})]

    assert asyncio.run(main()) == [1, 1, 2]


def test_langchain_tool_without_config(once: Once) -> None:
    @lc_tool(once)
    def add(a: int, b: int) -> int:
        return a + b

    assert add(1, 2) == 3


def test_bare_decorator_form(once: Once) -> None:
    def ping(n: int) -> int:
        return n

    assert mcp_tool(once, ping)(1) == 1
    assert lc_tool(once, ping)(2) == 2

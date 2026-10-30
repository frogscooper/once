"""``@once.tool(key=...)``: the caller picks the identity of a call."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from once import KeyConflict, Once, make_key


@pytest.fixture
def once(db: str | Path) -> Once:
    return Once(db)


def test_replay_with_different_args_returns_stored_result(once: Once) -> None:
    # Mycroft's case C: the replay re-plans the call with other args.
    refunds: list[tuple[str, int]] = []

    @once.tool(key=lambda a: a["order_id"])
    def refund(order_id: str, amount: int, memo: str = "") -> str:
        refunds.append((order_id, amount))
        return f"re_{len(refunds)}"

    with once.scope("run_1"):
        assert refund("o1", 500, memo="damaged") == "re_1"
        assert refund("o1", 450, memo="Refund for damaged item") == "re_1"
        assert refund(order_id="o1", amount=500) == "re_1"
    assert refunds == [("o1", 500)]


def test_different_key_values_run_separately(once: Once) -> None:
    calls: list[str] = []

    @once.tool(key=lambda a: a["order_id"])
    def refund(order_id: str, amount: int) -> str:
        calls.append(order_id)
        return f"re_{order_id}"

    assert refund("o1", 5) == "re_o1"
    assert refund("o2", 5) == "re_o2"
    assert refund("o1", 9) == "re_o1"
    assert calls == ["o1", "o2"]


def test_key_is_namespaced_by_tool_and_scope(once: Once) -> None:
    seen: list[str] = []

    @once.tool(key=lambda a: a["order_id"])
    def refund(order_id: str, idempotency_key: str | None = None) -> str:
        assert idempotency_key is not None
        seen.append(idempotency_key)
        return "refund"

    @once.tool(key=lambda a: a["order_id"])
    def cancel(order_id: str) -> str:
        return "cancel"

    assert refund("o1") == "refund"
    assert cancel("o1") == "cancel"
    with once.scope("run_2"):
        assert refund("o1") == "refund"
    assert seen == [
        make_key("refund", {"key": "o1"}, None),
        make_key("refund", {"key": "o1"}, "run_2"),
    ]


def test_key_gets_bound_args_with_defaults(once: Once) -> None:
    got: list[dict[str, Any]] = []

    def pick(args: dict[str, Any]) -> list[Any]:
        got.append(args)
        return [args["order_id"], args["currency"]]

    @once.tool(key=pick)
    def refund(order_id: str, amount: int, currency: str = "usd", idempotency_key: str = "") -> int:
        return amount

    assert refund("o1", amount=5) == 5
    assert got == [{"order_id": "o1", "amount": 5, "currency": "usd"}]


def test_other_args_need_not_be_json(once: Once) -> None:
    @once.tool(key=lambda a: a["order_id"])
    def refund(order_id: str, client: object) -> str:
        return "ok"

    assert refund("o1", object()) == "ok"
    assert refund("o1", object()) == "ok"


def test_idempotency_key_from_caller_wins(once: Once) -> None:
    calls: list[str | None] = []

    @once.tool(key=lambda a: a["order_id"])
    def refund(order_id: str, amount: int, idempotency_key: str | None = None) -> int:
        calls.append(idempotency_key)
        return len(calls)

    assert refund("o1", 5, idempotency_key="caller-1") == 1
    assert refund("o1", 5, idempotency_key="caller-1") == 1
    # Without it, key= picks a different key, so this runs.
    assert refund("o1", 5) == 2
    assert calls == ["caller-1", make_key("refund", {"key": "o1"}, None)]
    # With key=, the caller's key also checks only the value.
    assert refund("o1", 6, idempotency_key="caller-1") == 1
    with pytest.raises(KeyConflict):
        refund("o2", 5, idempotency_key="caller-1")


def test_execute_uses_key_fn_unless_given_a_key(once: Once) -> None:
    calls: list[int] = []

    @once.tool(key=lambda a: a["order_id"])
    def refund(order_id: str, amount: int) -> int:
        calls.append(amount)
        return amount

    assert once.execute(refund, order_id="o1", amount=5) == 5
    assert once.execute(refund, order_id="o1", amount=6) == 5
    assert once.execute(refund, key="manual", order_id="o1", amount=7) == 7
    assert calls == [5, 7]


def test_async_tool(once: Once) -> None:
    calls: list[int] = []

    @once.tool(key=lambda a: a["order_id"])
    async def refund(order_id: str, amount: int) -> int:
        calls.append(amount)
        return amount

    async def main() -> list[int]:
        return [
            await refund("o1", 5),
            await refund("o1", 6),
            await once.aexecute(refund, order_id="o1", amount=7),
            await refund("o2", 8),
        ]

    assert asyncio.run(main()) == [5, 5, 5, 8]
    assert calls == [5, 8]


def test_no_key_keeps_args_based_keys(once: Once) -> None:
    @once.tool
    def refund(order_id: str, amount: int, idempotency_key: str | None = None) -> str | None:
        return idempotency_key

    assert refund("o1", 5) == make_key("refund", {"order_id": "o1", "amount": 5}, None)


def test_key_and_exclude_together_is_an_error(once: Once) -> None:
    with pytest.raises(ValueError, match="key or exclude"):
        once.tool(key=lambda a: a["x"], exclude=("ctx",))


def test_key_must_be_callable(once: Once) -> None:
    with pytest.raises(TypeError, match="callable"):
        once.tool(key="order_id")  # type: ignore[call-overload]


@pytest.mark.parametrize("value", [None, "", [], {}])
def test_empty_key_value_is_an_error(once: Once, value: Any) -> None:
    calls: list[int] = []

    @once.tool(key=lambda a: value)
    def refund(order_id: str) -> None:
        calls.append(1)

    with pytest.raises(ValueError, match="non-empty"):
        refund("o1")
    assert calls == []
    assert once.records() == []


def test_key_fn_that_raises_is_an_error(once: Once) -> None:
    calls: list[int] = []

    @once.tool(key=lambda a: a["missing"])
    def refund(order_id: str) -> None:
        calls.append(1)

    with pytest.raises(ValueError, match="raised KeyError") as info:
        refund("o1")
    assert isinstance(info.value.__cause__, KeyError)
    assert calls == []
    assert once.records() == []


def test_key_value_must_be_json(once: Once) -> None:
    @once.tool(key=lambda a: object())
    def refund(order_id: str) -> None:
        pass

    with pytest.raises(TypeError, match="JSON"):
        refund("o1")
    assert once.records() == []


def test_async_key_fn_error_before_claim(once: Once) -> None:
    @once.tool(key=lambda a: None)
    async def refund(order_id: str) -> None:
        pass

    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(refund("o1"))
    assert once.records() == []


def test_caller_key_with_args_that_are_not_json(once: Once) -> None:
    class Payments:
        @once.tool(key=lambda a: a["order_id"])
        def refund(self, order_id: str, amount: int, idempotency_key: str = "") -> int:
            return amount

    payments = Payments()
    assert payments.refund("o1", 5, idempotency_key="caller-1") == 5
    assert payments.refund("o1", 6, idempotency_key="caller-1") == 5


def test_key_fn_changes_do_not_reach_the_call(once: Once) -> None:
    def pick(args: dict[str, Any]) -> str:
        args["order_id"] = "changed"
        args["items"].append("added")
        args["meta"]["note"] = "added"
        args["items"].sort()
        return "k"

    @once.tool(key=pick)
    def refund(order_id: str, items: list[str], meta: dict[str, Any]) -> list[Any]:
        return [order_id, items, meta]

    items, meta = ["b", "a"], {"x": [1]}
    assert refund("o1", items, meta) == ["o1", ["b", "a"], {"x": [1]}]
    assert items == ["b", "a"] and meta == {"x": [1]}


def test_key_fn_copy_handles_cycles_and_objects(once: Once) -> None:
    seen: list[Any] = []
    loop: list[Any] = [1]
    loop.append(loop)
    marker = object()

    def pick(args: dict[str, Any]) -> str:
        seen.append(args)
        return "k"

    @once.tool(key=pick)
    def refund(loop: list[Any], obj: object) -> int:
        return 1

    refund(loop, marker)
    copied = seen[0]["loop"]
    assert copied is not loop and copied[1] is copied
    assert seen[0]["obj"] is marker


def test_async_key_fn_is_rejected(once: Once) -> None:
    async def pick(args: dict[str, Any]) -> str:
        return "k"

    with pytest.raises(TypeError, match="not async"):
        once.tool(key=pick)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_execute_with_a_function_as_key(once: Once, asynchronous: bool) -> None:
    def refund(order_id: str) -> str:
        return order_id

    def pick(args: dict[str, Any]) -> str:
        return str(args["order_id"])

    with pytest.raises(TypeError, match=r"once\.tool"):
        if asynchronous:
            asyncio.run(once.aexecute(refund, key=pick, order_id="o1"))  # type: ignore[arg-type]
        else:
            once.execute(refund, key=pick, order_id="o1")  # type: ignore[arg-type]
    assert once.records() == []

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import pytest

from once import EffectFailed, KeyConflict, Once, Status, current_scope, make_key


@pytest.fixture
def once(db: str | Path) -> Once:
    return Once(db)


def test_make_key_formula() -> None:
    expected = hashlib.sha256(b'["charge_card",{"amount":1000,"customer":"cus_123"},"order_456"]')
    key = make_key("charge_card", {"customer": "cus_123", "amount": 1000}, scope="order_456")
    assert key == expected.hexdigest()


def test_make_key_is_deterministic_and_scoped() -> None:
    a = make_key("send", {"to": "a@x.com", "n": 1})
    assert a == make_key("send", {"n": 1, "to": "a@x.com"})
    assert len(a) == 64
    assert a != make_key("send", {"to": "a@x.com", "n": 1}, scope="run_1")
    assert make_key("send", {}, scope="run_1") != make_key("send", {}, scope="run_2")
    assert make_key("send", {}) != make_key("post", {})


def test_side_effect_runs_once_and_replay_returns_stored_result(once: Once) -> None:
    calls: list[str] = []

    @once.tool
    def send_email(to: str, subject: str) -> dict[str, str]:
        calls.append(to)
        return {"id": f"msg_{len(calls)}"}

    first = send_email("a@x.com", "hi")
    again = send_email(to="a@x.com", subject="hi")
    assert first == again == {"id": "msg_1"}
    assert calls == ["a@x.com"]

    send_email("b@x.com", "hi")
    assert calls == ["a@x.com", "b@x.com"]


def test_scope_changes_the_key(once: Once) -> None:
    calls = 0

    @once.tool(name="post")
    def post(text: str) -> int:
        nonlocal calls
        calls += 1
        return calls

    with once.scope("run_1"):
        assert post("hello") == 1
        assert post("hello") == 1
    with once.scope("run_2"):
        assert post("hello") == 2
    assert post("hello") == 3


def test_current_scope(once: Once) -> None:
    assert current_scope() is None
    with once.scope("run_1"):
        assert current_scope() == "run_1"
        with once.scope("step_2"):
            assert current_scope() == "step_2"
        with once.scope(None):
            assert current_scope() is None
        assert current_scope() == "run_1"
    assert current_scope() is None


def test_current_scope_follows_async_tasks(once: Once) -> None:
    async def inner() -> str | None:
        await asyncio.sleep(0)
        return current_scope()

    async def main() -> list[str | None]:
        with once.scope("a"):
            task = asyncio.create_task(inner())
        with once.scope("b"):
            return [await task, await inner()]

    assert asyncio.run(main()) == ["a", "b"]


def test_idempotency_key_is_passed_through(once: Once) -> None:
    seen: list[str | None] = []

    @once.tool(name="charge_card")
    def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> str:
        seen.append(idempotency_key)
        return "ch_1"

    with once.scope("order_456"):
        charge_card(1000, "cus_123")
    expected = make_key("charge_card", {"amount": 1000, "customer": "cus_123"}, "order_456")
    assert seen == [expected]


def test_execute_matches_decorator_key(once: Once) -> None:
    calls = 0

    @once.tool(name="charge_card")
    def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> str:
        nonlocal calls
        calls += 1
        return "ch_1"

    key = make_key("charge_card", {"amount": 1000, "customer": "cus_123"}, scope="order_456")
    assert once.execute(key=key, fn=charge_card, amount=1000, customer="cus_123") == "ch_1"
    with once.scope("order_456"):
        assert charge_card(1000, "cus_123") == "ch_1"
    assert once.execute(fn=charge_card, scope="order_456", amount=1000, customer="cus_123")
    assert calls == 1
    record = once.get(key)
    assert record is not None and record.status is Status.DONE


def test_explicit_idempotency_key_argument_is_the_key(once: Once) -> None:
    calls = 0

    @once.tool
    def create_ticket(title: str, idempotency_key: str | None = None) -> str:
        nonlocal calls
        calls += 1
        return f"T-{calls}"

    assert create_ticket("bug", idempotency_key="k1") == "T-1"
    assert create_ticket("bug", idempotency_key="k1") == "T-1"
    assert create_ticket("bug", idempotency_key="k2") == "T-2"
    assert once.get("k1") is not None


@pytest.mark.parametrize("empty", ["", None])
def test_empty_idempotency_key_means_compute_one(once: Once, empty: str | None) -> None:
    # Regression: a default of "" was used as the key, so two tools with
    # that default collided with KeyConflict.
    seen: list[str] = []

    @once.tool
    def send_email(to: str, idempotency_key: str = "") -> str:
        seen.append(idempotency_key)
        return f"email to {to}"

    @once.tool
    def send_sms(to: str, idempotency_key: str = "") -> str:
        seen.append(idempotency_key)
        return f"sms to {to}"

    assert send_email("a") == "email to a"
    assert send_sms("a") == "sms to a"
    assert send_email("b", idempotency_key=empty) == "email to b"  # type: ignore[arg-type]
    assert once.execute(send_sms, to="b", idempotency_key=empty) == "sms to b"
    assert seen == [
        make_key("send_email", {"to": "a"}),
        make_key("send_sms", {"to": "a"}),
        make_key("send_email", {"to": "b"}),
        make_key("send_sms", {"to": "b"}),
    ]
    assert once.get("") is None
    assert send_email("a", idempotency_key=empty) == "email to a"  # type: ignore[arg-type]
    assert len(seen) == 4


def test_key_reuse_with_different_args_raises(once: Once) -> None:
    def charge(amount: int) -> int:
        return amount

    assert once.execute(charge, key="k", amount=100) == 100
    with pytest.raises(KeyConflict):
        once.execute(charge, key="k", amount=200)


def test_known_failure_is_stored_and_not_retried(once: Once) -> None:
    calls = 0

    @once.tool
    def charge(amount: int) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("card declined")

    with pytest.raises(ValueError, match="card declined"):
        charge(100)
    with pytest.raises(EffectFailed, match="card declined") as info:
        charge(100)
    assert info.value.record.status is Status.FAILED
    assert calls == 1


def test_reset_allows_a_rerun(once: Once) -> None:
    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return "ok"

    with pytest.raises(RuntimeError):
        once.execute(flaky, key="k")
    assert once.reset("k")
    assert once.execute(flaky, key="k") == "ok"


def test_records_persist_across_instances(db: str | Path) -> None:
    calls = 0

    def send() -> list[int]:
        nonlocal calls
        calls += 1
        return [1, 2]

    assert Once(db).execute(send, key="k") == [1, 2]
    assert Once(db).execute(send, key="k") == [1, 2]
    assert calls == 1


def test_first_call_returns_json_round_tripped_value(once: Once) -> None:
    def f() -> tuple[int, int]:
        return (1, 2)

    assert once.execute(f, key="k") == [1, 2]
    assert once.execute(f, key="k") == [1, 2]


def test_non_json_args_are_rejected_before_running(once: Once) -> None:
    ran = False

    def f(x: object) -> None:
        nonlocal ran
        ran = True

    with pytest.raises(TypeError, match="argument 'x' to 'f' must be JSON serializable"):
        once.execute(f, x=object())
    assert not ran

    def g(a: int, b: dict[str, float]) -> None:
        pass

    with pytest.raises(TypeError, match="argument 'b' to 'g'"):
        once.execute(g, a=1, b={"n": float("nan")})


def test_exclude_leaves_params_out_of_the_key(once: Once) -> None:
    seen: list[object] = []

    @once.tool(exclude=("ctx",))
    def f(x: int, ctx: object) -> int:
        seen.append(ctx)
        return x

    a, b = object(), object()
    assert f(1, a) == 1
    assert f(1, b) == 1
    assert seen == [a]
    assert once.execute(f, x=1, ctx=b) == 1  # decorated tool keeps its exclude
    assert len(seen) == 1


def test_records_and_purge_api(db: str | Path) -> None:
    with Once(db) as once:
        once.execute(lambda: 1, key="a")
        once.execute(lambda: 2, key="b")
        assert {r.key for r in once.records()} == {"a", "b"}
        assert once.records(status="pending") == []
        assert len(once.records(status=["done", Status.FAILED])) == 2
        assert once.purge(3600) == 0
        assert once.purge(0, status=[]) == 0
        time.sleep(0.05)
        assert once.purge(0) == 2

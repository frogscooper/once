from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from once import EffectFailed, KeyConflict, Once, Status, make_key


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "once.db"


@pytest.fixture
def once(db: Path) -> Once:
    return Once(db)


def test_make_key_formula() -> None:
    expected = hashlib.sha256(b'charge_card{"amount":1000,"customer":"cus_123"}order_456')
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


def test_records_persist_across_instances(db: Path) -> None:
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

    with pytest.raises(TypeError, match="JSON serializable"):
        once.execute(f, x=object())
    assert not ran


def test_async_functions_are_rejected(once: Once) -> None:
    async def f() -> None:
        pass

    with pytest.raises(TypeError, match="async"):
        once.tool(f)

"""Intents: id fields pick the record, matchers decide whether a replay is the same act."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from once import (
    NOT_HAPPENED,
    EffectUnknown,
    KeyConflict,
    Once,
    Record,
    SQLiteStore,
    equal_after,
    exact,
    ignore,
    intent,
    make_key,
    within,
)
from once.cli import main

REFUND = intent(
    "refund",
    id={"order_id": str},
    match={"amount": within(pct=1), "memo": ignore},
)


def refunder(once: Once, ran: list[Any]) -> Any:
    @once.tool(intent=REFUND)
    def refund(order_id: Any, amount: int, currency: str = "usd", memo: Any = "") -> str:
        ran.append((order_id, amount))
        return f"re_{len(ran)}"

    return refund


def test_replays_within_the_matchers(db: str | Path) -> None:
    ran: list[Any] = []
    with Once(db) as once:
        refund = refunder(once, ran)
        assert refund("o1", 500, memo="damaged") == "re_1"
        # id normalized by str, amount within 1%, memo ignored and not JSON
        assert refund("o1", 504, memo=object()) == "re_1"
        assert ran == [("o1", 500)]
        record = once.records()[0]
        assert json.loads(record.args or "") == {"amount": 500, "currency": "usd"}


def test_a_normalizer_makes_numbers_and_strings_the_same_id(db: str | Path) -> None:
    ran: list[Any] = []
    with Once(db) as once:
        refund = refunder(once, ran)
        refund(123, 500)
        assert refund("123", 500) == "re_1"
        assert len(ran) == 1


def test_a_mismatch_names_the_fields(db: str | Path) -> None:
    ran: list[Any] = []
    with Once(db) as once:
        refund = refunder(once, ran)
        refund("o1", 500)
        with pytest.raises(KeyConflict) as info:
            refund("o1", 450, currency="eur")
        detail = info.value.detail
        assert detail is not None
        assert "amount: stored 500, got 450 (within(pct=1))" in detail
        assert "currency: stored 'usd', got 'eur' (exact)" in detail
        assert "amount" in str(info.value)
        assert len(ran) == 1


def test_tolerance_is_anchored_to_the_first_call(db: str | Path) -> None:
    # 500 ~ 504 and 504 ~ 509, but 500 !~ 509: a replay never moves the anchor.
    ran: list[Any] = []
    with Once(db) as once:
        refund = refunder(once, ran)
        refund("o1", 500)
        assert refund("o1", 504) == "re_1"
        with pytest.raises(KeyConflict, match="amount"):
            refund("o1", 509)
        assert json.loads(once.records()[0].args or "")["amount"] == 500


def test_the_key_comes_from_the_intent_not_the_function(tmp_path: Path) -> None:
    with Once(tmp_path / "n.db") as once:

        @once.tool(intent=REFUND)
        def old_name(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> str:
            return "first"

        @once.tool(intent=REFUND)
        def renamed(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> str:
            return "second"

        assert old_name("o1", 500) == "first"
        assert renamed("o1", 500) == "first"
        record = once.records()[0]
        assert record.key == make_key("refund@1", {"id": {"order_id": "o1"}}, None)


def test_a_new_version_does_not_replay_old_records(tmp_path: Path) -> None:
    v2 = intent("refund", id=["order_id"], match={"amount": exact}, version=2)
    with Once(tmp_path / "v.db") as once:
        ran: list[Any] = []
        refund = refunder(once, ran)

        @once.tool(intent=v2)
        def refund_v2(order_id: str, amount: int) -> str:
            return "v2"

        assert refund("o1", 500) == "re_1"
        assert refund_v2("o1", 500) == "v2"
        assert len(once.records()) == 2


def test_scope_and_explicit_key_still_apply(tmp_path: Path) -> None:
    ran: list[Any] = []
    with Once(tmp_path / "s.db") as once:
        refund = refunder(once, ran)
        with once.scope("run-1"):
            refund("o1", 500)
        with once.scope("run-2"):
            refund("o1", 500)
        assert len(ran) == 2

        @once.tool(intent=intent("charge", id=["customer"]))
        def charge(customer: str, amount: int, idempotency_key: str | None = None) -> str:
            return str(idempotency_key)

        assert charge("c1", 5, idempotency_key="mine") == "mine"
        assert charge("c1", 5, idempotency_key="mine") == "mine"
        with pytest.raises(KeyConflict, match="amount: stored 5, got 6"):
            charge("c1", 6, idempotency_key="mine")
        with pytest.raises(KeyConflict):  # another id under the same key
            charge("c2", 5, idempotency_key="mine")


def test_execute_uses_the_tool_intent(tmp_path: Path) -> None:
    ran: list[Any] = []
    with Once(tmp_path / "e.db") as once:
        refund = refunder(once, ran)
        refund("o1", 500)
        assert once.execute(refund, order_id="o1", amount=501) == "re_1"
        assert len(ran) == 1


def test_async_tools(tmp_path: Path) -> None:
    ran: list[int] = []
    with Once(tmp_path / "a.db") as once:

        @once.tool(intent=intent("pay", id=["invoice"], match={"amount": within(abs=2)}))
        async def pay(invoice: str, amount: int) -> str:
            ran.append(amount)
            return "paid"

        async def main_() -> None:
            assert await pay("i1", 100) == "paid"
            assert await pay("i1", 102) == "paid"
            with pytest.raises(KeyConflict, match="within"):
                await pay("i1", 103)

        asyncio.run(main_())
        assert ran == [100]


def test_an_expired_record_stores_the_new_args(tmp_path: Path) -> None:
    ran: list[Any] = []
    with Once(tmp_path / "t.db") as once:

        @once.tool(intent=REFUND, ttl=timedelta(microseconds=1))
        def refund(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> str:
            ran.append(amount)
            return "ok"

        refund("o1", 500)

        time.sleep(0.01)
        refund("o1", 900)  # expired, so it runs again with any amount
        assert ran == [500, 900]
        assert json.loads(once.records()[0].args or "")["amount"] == 900


def test_verify_gets_the_stored_args(tmp_path: Path) -> None:
    seen: list[Any] = []

    def verify(key: str, args: dict[str, Any], stored: Any) -> Any:
        seen.append((args["amount"], stored))
        return NOT_HAPPENED

    with Once(tmp_path / "v.db") as once:
        calls: list[int] = []

        @once.tool(intent=REFUND, verify=verify)
        def refund(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> str:
            calls.append(amount)
            if len(calls) == 1:
                raise TimeoutError
            return "ok"

        with pytest.raises(TimeoutError):
            refund("o1", 500)
        assert refund("o1", 503) == "ok"
        assert seen == [(503, {"amount": 500, "currency": "usd"})]


def test_a_verify_without_stored_is_called_as_before(tmp_path: Path) -> None:
    with Once(tmp_path / "v.db") as once:

        @once.tool(intent=REFUND, verify=lambda key, args: {"found": args["amount"]})
        def refund(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> Any:
            raise TimeoutError

        with pytest.raises(TimeoutError):
            refund("o1", 500)
        assert refund("o1", 500) == {"found": 500}


def test_a_record_without_stored_args_is_a_conflict(tmp_path: Path) -> None:
    ran: list[Any] = []
    with Once(tmp_path / "x.db") as once:
        refund = refunder(once, ran)
        refund("o1", 500)
        key = once.records()[0].key
        once.store._conn().execute("UPDATE effects SET args = NULL")  # type: ignore[attr-defined]
        with pytest.raises(KeyConflict, match="no arguments to compare"):
            refund("o1", 500)
        assert once.get(key) is not None


def test_unknown_records_still_raise(tmp_path: Path) -> None:
    with Once(tmp_path / "u.db") as once:

        @once.tool(intent=REFUND)
        def refund(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> str:
            raise TimeoutError

        with pytest.raises(TimeoutError):
            refund("o1", 500)
        with pytest.raises(EffectUnknown):
            refund("o1", 500)
        with pytest.raises(KeyConflict):  # a conflict is reported before the status
            refund("o1", 999)


class LegacyStore:
    """A store written before ``args`` existed: claim and renew take two arguments."""

    def __init__(self, path: Path) -> None:
        self.inner = SQLiteStore(path)

    def claim(self, key: str, args_hash: str | None) -> str | None:
        return self.inner.claim(key, args_hash)

    def renew(self, record: Record, args_hash: str | None) -> str | None:
        return self.inner.renew(record, args_hash)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_a_store_without_args_still_works_for_plain_tools(tmp_path: Path) -> None:
    once = Once(store=LegacyStore(tmp_path / "l.db"))  # type: ignore[arg-type]
    assert once.execute(lambda: "ok", key="k") == "ok"
    assert once.execute(lambda: "again", key="k") == "ok"
    ran: list[Any] = []
    with pytest.raises(TypeError, match="args"):
        refunder(once, ran)("o1", 500)
    once.close()


def test_cli_show_prints_the_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.db"
    with Once(path) as once:
        refunder(once, [])("o1", 500)
        key = once.records()[0].key
    assert main(["show", "--db", str(path), key[:8]]) == 0
    assert json.loads(capsys.readouterr().out)["args"] == {"amount": 500, "currency": "usd"}


def test_bad_intents_are_rejected(tmp_path: Path) -> None:
    once = Once(tmp_path / "b.db")

    def f(a: int, b: int, ctx: Any = None, idempotency_key: str | None = None) -> None: ...

    cases: list[tuple[dict[str, Any], str]] = [
        ({"id": []}, "at least one id"),
        ({"id": ["a"], "match": {"a": exact}}, "in both id and match"),
        ({"id": "a"}, "not a str"),
        ({"id": ["a"], "version": 0}, "version"),
        ({"id": {"a": 5}}, "not callable"),
        ({"id": ["a"], "match": {"b": 5}}, "not callable"),
    ]
    for kwargs, error in cases:
        with pytest.raises((ValueError, TypeError), match=error):
            intent("t", **kwargs)
    for name in ("", "a@b"):
        with pytest.raises(ValueError):
            intent(name, id=["a"])
    for fields in (["nope"], ["idempotency_key"]):
        with pytest.raises(ValueError, match="not parameters"):
            once.tool(intent=intent("t", id=fields))(f)
    with pytest.raises(ValueError, match="not parameters"):
        once.tool(intent=intent("t", id=["ctx"]), exclude=["ctx"])(f)
    with pytest.raises(ValueError, match="key or intent"):
        once.tool(intent=intent("t", id=["a"]), key=lambda a: a["a"])(f)
    with pytest.raises(ValueError, match="name or intent"):
        once.tool(intent=intent("t", id=["a"]), name="t")(f)
    with pytest.raises(TypeError, match=r"once.intent"):
        once.tool(intent="t")(f)  # type: ignore[call-overload]
    with pytest.raises(ValueError, match="abs=, pct="):
        within()
    with pytest.raises(ValueError):
        within(pct=-1)

    @once.tool(intent=intent("t", id=["a"]))
    def g(a: Any, b: Any) -> None: ...

    with pytest.raises(ValueError, match="'a' is None"):
        g(None, 1)
    with pytest.raises(TypeError, match="argument 'b'"):
        g(1, object())
    once.close()


def test_matchers() -> None:
    assert within(abs=1)(10, 11) and not within(abs=1)(10, 11.5)
    assert within(pct=10)(100, 90) and not within(pct=10)(100, 89)
    assert within(abs=5, pct=1)(100, 104)  # either is enough
    assert not within(abs=1)(True, 1)  # bools aren't numbers
    assert within(abs=1)("x", "x") and not within(abs=1)("x", "y")
    assert not within(abs=1)(1, float("inf"))
    assert equal_after(str.strip)(" a ", "a") and not equal_after(str.strip)("a", "b")
    assert not equal_after(str.strip)(1, 1)  # fn raised
    assert exact([1, 2], (1, 2)) and not exact(1, "1")
    assert ignore(object(), None)
    assert repr(within(abs=1, pct=2)) == "within(abs=1, pct=2)"


# properties

ids = st.text(min_size=1, max_size=8)
amounts = st.integers(min_value=1, max_value=10**6)


@settings(max_examples=60, deadline=None)
@given(ids, amounts, st.integers(min_value=-(10**6), max_value=10**6))
def test_replay_if_and_only_if_within(order_id: str, first: int, delta: int) -> None:
    second = first + delta
    matcher = within(pct=1)
    with tempfile.TemporaryDirectory() as d, Once(Path(d) / "p.db") as once:
        ran: list[Any] = []
        refund = refunder(once, ran)
        refund(order_id, first)
        if matcher(first, second):
            assert refund(order_id, second) == "re_1"
        else:
            with pytest.raises(KeyConflict, match="amount"):
                refund(order_id, second)
        assert len(ran) == 1
        assert json.loads(once.records()[0].args or "")["amount"] == first


@settings(max_examples=60, deadline=None)
@given(amounts, st.lists(st.integers(min_value=-20, max_value=20), max_size=6))
def test_replays_never_move_the_anchor(first: int, deltas: list[int]) -> None:
    with tempfile.TemporaryDirectory() as d, Once(Path(d) / "p.db") as once:
        refund = refunder(once, [])
        refund("o", first)
        for delta in deltas:
            with contextlib.suppress(KeyConflict):
                refund("o", first + delta)
            assert json.loads(once.records()[0].args or "")["amount"] == first


normalizers = [str, str.lower, str.strip, lambda s: s.casefold().strip()]


@given(st.sampled_from(normalizers), st.text(max_size=10))
def test_builtin_style_normalizers_are_idempotent(normalize: Any, value: str) -> None:
    once_ = normalize(value)
    assert normalize(once_) == once_


def test_errors_from_user_code_name_the_field(tmp_path: Path) -> None:
    def boom(stored: Any, new: Any) -> bool:
        raise RuntimeError("bad matcher")

    with Once(tmp_path / "f.db") as once:

        @once.tool(intent=intent("t", id={"a": int}, match={"b": boom}))
        def t(a: Any, b: int) -> int:
            return b

        with pytest.raises(ValueError, match="normalizing 'a'"):
            t("x", 1)
        assert t("1", 1) == 1
        with pytest.raises(ValueError, match="matcher for 'b' raised"):
            t(1, 1)

        @once.tool(intent=intent("u", id={"a": lambda v: object()}))
        def u(a: int) -> int:
            return a

        with pytest.raises(TypeError, match="JSON after normalizing"):
            u(1)


def test_a_field_added_later_is_reported_missing(tmp_path: Path) -> None:
    spec = intent("send", id=["to"])
    with Once(tmp_path / "m.db") as once:

        @once.tool(intent=spec)
        def send(to: str) -> str:
            return "v1"

        send("a")

        @once.tool(intent=spec)
        def send_v2(to: str, cc: str = "") -> str:
            return "v2"

        with pytest.raises(KeyConflict, match="cc: missing from the stored call"):
            send_v2("a")


def test_a_custom_matcher_class() -> None:
    from once import Matcher

    class Caseless(Matcher):
        def __call__(self, stored: Any, new: Any) -> bool:
            return bool(str(stored).lower() == str(new).lower())

    assert Caseless()("A", "a") and repr(Caseless()) == "Caseless"
    with pytest.raises(NotImplementedError):
        Matcher()(1, 1)

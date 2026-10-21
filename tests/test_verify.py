from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from once import (
    NOT_HAPPENED,
    EffectFailed,
    EffectPending,
    EffectUnknown,
    Once,
    Status,
    args_hash,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "once.db"


@pytest.fixture
def once(db: Path) -> Once:
    return Once(db)


class Charger:
    """A tool that times out on its first call, with a counter."""

    def __init__(self, fail_first: bool = True) -> None:
        self.calls = 0
        self.fail_first = fail_first

    def __call__(self, amount: int) -> str:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise TimeoutError("read timed out")
        return f"ch_{self.calls}"


def make_unknown(once: Once, charger: Charger) -> None:
    with pytest.raises(TimeoutError):
        once.execute(charger, key="k", tool_name="charge", amount=100)
    record = once.get("k")
    assert record is not None and record.status is Status.UNKNOWN


def test_unknown_without_verify_is_never_rerun(once: Once) -> None:
    charger = Charger()
    make_unknown(once, charger)
    with pytest.raises(EffectUnknown):
        once.execute(charger, key="k", tool_name="charge", amount=100)
    assert charger.calls == 1


def test_verify_finds_the_effect(once: Once) -> None:
    charger = Charger()
    make_unknown(once, charger)
    seen: list[tuple[str, dict[str, Any]]] = []

    def verify(key: str, args: dict[str, Any]) -> Any:
        seen.append((key, args))
        return "ch_found"

    result = once.execute(charger, key="k", tool_name="charge", verify=verify, amount=100)
    assert result == "ch_found"
    assert seen == [("k", {"amount": 100})]
    assert charger.calls == 1
    record = once.get("k")
    assert record is not None and record.status is Status.DONE

    # now it is a plain stored result, verify is not asked again
    assert once.execute(charger, key="k", tool_name="charge", verify=verify, amount=100) == (
        "ch_found"
    )
    assert len(seen) == 1


def test_verify_says_not_happened_so_it_reruns(once: Once) -> None:
    charger = Charger()
    make_unknown(once, charger)

    def verify(key: str, args: dict[str, Any]) -> Any:
        return NOT_HAPPENED

    assert once.execute(charger, key="k", tool_name="charge", verify=verify, amount=100) == "ch_2"
    assert charger.calls == 2
    assert once.execute(charger, key="k", tool_name="charge", verify=verify, amount=100) == "ch_2"
    assert charger.calls == 2


def test_verify_error_leaves_record_unknown(once: Once) -> None:
    charger = Charger()
    make_unknown(once, charger)

    def verify(key: str, args: dict[str, Any]) -> Any:
        raise ConnectionError("stripe is down")

    with pytest.raises(ConnectionError):
        once.execute(charger, key="k", tool_name="charge", verify=verify, amount=100)
    record = once.get("k")
    assert record is not None and record.status is Status.UNKNOWN
    assert charger.calls == 1


def test_verify_result_must_be_json(once: Once) -> None:
    charger = Charger()
    make_unknown(once, charger)

    def verify(key: str, args: dict[str, Any]) -> Any:
        return object()

    with pytest.raises(TypeError):
        once.execute(charger, key="k", tool_name="charge", verify=verify, amount=100)
    record = once.get("k")
    assert record is not None and record.status is Status.UNKNOWN


def test_failed_is_not_verified(once: Once) -> None:
    def charge(amount: int) -> None:
        raise ValueError("declined")

    def verify(key: str, args: dict[str, Any]) -> Any:
        raise AssertionError("should not be called")

    with pytest.raises(ValueError):
        once.execute(charge, key="k", amount=1)
    with pytest.raises(EffectFailed):
        once.execute(charge, key="k", verify=verify, amount=1)


def claim_pending(db: Path, tool: str, args: dict[str, Any]) -> str:
    token = Once(db).store.claim("k", args_hash({"tool": tool, "args": args}))
    assert token is not None
    return token


def test_fresh_pending_is_not_verified(db: Path) -> None:
    claim_pending(db, "charge", {"amount": 100})
    charger = Charger(fail_first=False)

    def verify(key: str, args: dict[str, Any]) -> Any:
        raise AssertionError("should not be called")

    o = Once(db, stale_after=60)
    with pytest.raises(EffectPending):
        o.execute(charger, key="k", tool_name="charge", verify=verify, amount=100)
    # without stale_after a pending record is never verified
    with pytest.raises(EffectPending):
        Once(db).execute(charger, key="k", tool_name="charge", verify=verify, amount=100)
    assert charger.calls == 0


def test_stale_pending_is_verified_and_old_owner_cannot_overwrite(db: Path) -> None:
    old_token = claim_pending(db, "charge", {"amount": 100})
    time.sleep(0.05)
    charger = Charger(fail_first=False)

    def verify(key: str, args: dict[str, Any]) -> Any:
        return NOT_HAPPENED

    o = Once(db, stale_after=0.01)
    assert o.execute(charger, key="k", tool_name="charge", verify=verify, amount=100) == "ch_1"

    # the original owner wakes up late and tries to record its outcome
    assert not o.store.finish("k", old_token, Status.FAILED, error="late")
    record = o.get("k")
    assert record is not None and record.status is Status.DONE
    assert record.result == '"ch_1"'


def test_decorator_verify_is_used(once: Once) -> None:
    calls = 0

    def verify(key: str, args: dict[str, Any]) -> Any:
        return {"id": "msg_found"}

    @once.tool(verify=verify)
    def send_email(to: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        raise TimeoutError

    with pytest.raises(TimeoutError):
        send_email("a@x.com")
    assert send_email("a@x.com") == {"id": "msg_found"}
    assert calls == 1

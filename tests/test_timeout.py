from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from once import EffectUnknown, Once, Status


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "once.db"


@pytest.fixture
def once(db: Path) -> Once:
    return Once(db)


def test_timeout_error_marks_unknown_and_never_reruns(once: Once) -> None:
    calls = 0

    @once.tool
    def send_email(to: str) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("smtp read timed out")

    with pytest.raises(TimeoutError):
        send_email("a@x.com")
    with pytest.raises(EffectUnknown, match="smtp read timed out") as info:
        send_email("a@x.com")
    assert info.value.record.status is Status.UNKNOWN
    assert calls == 1


def test_timeout_argument_marks_unknown(once: Once) -> None:
    release = threading.Event()
    calls = 0

    def slow() -> str:
        nonlocal calls
        calls += 1
        release.wait(5)
        return "sent"

    started = time.monotonic()
    with pytest.raises(EffectUnknown, match="timeout"):
        once.execute(slow, key="k", timeout=0.1)
    assert time.monotonic() - started < 2
    release.set()

    with pytest.raises(EffectUnknown):
        once.execute(slow, key="k", timeout=0.1)
    assert calls == 1
    record = once.get("k")
    assert record is not None and record.status is Status.UNKNOWN


def test_decorator_timeout(once: Once) -> None:
    @once.tool(timeout=0.05)
    def slow() -> None:
        time.sleep(1)

    with pytest.raises(EffectUnknown):
        slow()


def test_fast_call_under_timeout_succeeds(once: Once) -> None:
    assert once.execute(lambda: "ok", key="k", timeout=2) == "ok"
    assert once.execute(lambda: "ok", key="k", timeout=2) == "ok"


def test_custom_unknown_exceptions(db: Path) -> None:
    class ConnectionDropped(Exception):
        pass

    o = Once(db, unknown_on=(TimeoutError, ConnectionDropped))

    def f() -> None:
        raise ConnectionDropped("reset by peer")

    with pytest.raises(ConnectionDropped):
        o.execute(f, key="k")
    record = o.get("k")
    assert record is not None and record.status is Status.UNKNOWN


def test_interrupt_marks_unknown(once: Once) -> None:
    def f() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        once.execute(f, key="k")
    record = once.get("k")
    assert record is not None and record.status is Status.UNKNOWN


def test_unserializable_result_marks_unknown(once: Once) -> None:
    calls = 0

    def f() -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(EffectUnknown, match="not JSON serializable"):
        once.execute(f, key="k")
    with pytest.raises(EffectUnknown):
        once.execute(f, key="k")
    assert calls == 1

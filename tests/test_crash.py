"""A process killed in the middle of a call leaves its record pending."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from once import NOT_HAPPENED, EffectPending, Once, Status

SRC = str(Path(__file__).resolve().parents[1] / "src")

WORKER = """
import sys, time
sys.path.insert(0, {src!r})
from once import Once

def charge(amount):
    with open({log!r}, "a") as f:
        f.write("x")
    open({marker!r}, "w").close()
    time.sleep(60)
    return "never"

Once({db!r}).execute(charge, key="k", tool_name="charge", amount=1)
"""


@pytest.fixture
def killed(db: str | Path, tmp_path: Path) -> tuple[str | Path, Path]:
    """Run a call in a subprocess and kill it after the side effect happened."""
    log = tmp_path / "effects.log"
    marker = tmp_path / "started"
    script = WORKER.format(src=SRC, db=str(db), log=str(log), marker=str(marker))
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert proc.poll() is None, "worker exited early"
            assert time.monotonic() < deadline, "worker never started"
            time.sleep(0.02)
    finally:
        proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
        proc.wait(10)
    assert log.read_text() == "x"
    return db, log


def charge(amount: int) -> str:
    raise AssertionError("must not run again")


def test_killed_call_stays_pending(killed: tuple[str | Path, Path]) -> None:
    db, _ = killed
    with Once(db) as once:
        record = once.get("k")
        assert record is not None and record.status is Status.PENDING
        with pytest.raises(EffectPending):
            once.execute(charge, key="k", tool_name="charge", amount=1)


def test_stale_without_verify_never_reruns(killed: tuple[str | Path, Path]) -> None:
    db, log = killed
    with Once(db, stale_after=0) as once, pytest.raises(EffectPending):
        once.execute(charge, key="k", tool_name="charge", amount=1)
    assert log.read_text() == "x"


def test_verify_is_not_asked_before_stale_after(killed: tuple[str | Path, Path]) -> None:
    db, _ = killed

    def verify(key: str, args: dict[str, Any]) -> Any:
        raise AssertionError("too early")

    with Once(db, stale_after=60) as once, pytest.raises(EffectPending):
        once.execute(charge, key="k", tool_name="charge", amount=1, verify=verify)


def test_verify_recovers_the_result(killed: tuple[str | Path, Path]) -> None:
    db, log = killed

    def verify(key: str, args: dict[str, Any]) -> Any:
        assert log.read_text() == "x"  # the effect happened
        return "ch_found"

    with Once(db, stale_after=0) as once:
        assert once.execute(charge, key="k", tool_name="charge", amount=1, verify=verify) == (
            "ch_found"
        )
        record = once.get("k")
        assert record is not None and record.status is Status.DONE


def test_verify_not_happened_reruns_once(killed: tuple[str | Path, Path]) -> None:
    db, _ = killed
    calls = 0

    def charge_again(amount: int) -> str:
        nonlocal calls
        calls += 1
        return "ch_2"

    def verify(key: str, args: dict[str, Any]) -> Any:
        return NOT_HAPPENED

    with Once(db, stale_after=0) as once:
        for _ in range(3):
            result = once.execute(
                charge_again, key="k", tool_name="charge", amount=1, verify=verify
            )
            assert result == "ch_2"
    assert calls == 1

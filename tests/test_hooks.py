"""Lifecycle hooks: on_claim, on_done, on_failed, on_unknown, on_replay."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from once import (
    NOT_HAPPENED,
    EffectFailed,
    EffectUnknown,
    Event,
    Hooks,
    KeyConflict,
    Once,
    Status,
)

KINDS = ("claim", "done", "failed", "unknown", "replay")


class Log:
    """Hooks that record every event."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def hooks(self) -> Hooks:
        return Hooks(**{f"on_{kind}": self.events.append for kind in KINDS})

    def seen(self) -> list[tuple[str, str, Status]]:
        return [(e.kind, e.outcome, e.status) for e in self.events]


def charge(amount: int) -> str:
    return f"ch_{amount}"


def test_run_then_replay(db: str | Path) -> None:
    log = Log()
    before = time.time()
    with Once(db, hooks=log.hooks()) as once:
        assert once.execute(charge, amount=5) == "ch_5"
        assert once.execute(charge, amount=5) == "ch_5"
    assert log.seen() == [
        ("claim", "ran", Status.PENDING),
        ("done", "ran", Status.DONE),
        ("replay", "replayed", Status.DONE),
    ]
    claim, done, replay = log.events
    assert {e.tool_name for e in log.events} == {"charge"}
    assert claim.key == done.key == replay.key
    assert claim.record is not None and claim.record.status is Status.PENDING
    assert done.record is not None and done.record.result == '"ch_5"'
    assert replay.record is not None and replay.record.status is Status.DONE
    assert all(before <= e.started_at <= time.time() and e.duration >= 0 for e in log.events)
    assert done.error is None


def test_tool_decorator(db: str | Path) -> None:
    log = Log()
    with Once(db, hooks=log.hooks()) as once:

        @once.tool(name="send")
        def send(to: str) -> str:
            return to

        send("a")
        send("a")
    assert [(e.kind, e.tool_name) for e in log.events] == [
        ("claim", "send"),
        ("done", "send"),
        ("replay", "send"),
    ]


def test_failed(db: str | Path) -> None:
    log = Log()

    def boom() -> None:
        raise ValueError("card declined")

    with Once(db, hooks=log.hooks()) as once:
        with pytest.raises(ValueError):
            once.execute(boom, key="k")
        with pytest.raises(EffectFailed):
            once.execute(boom, key="k")  # a stored failure is not a replay
    assert log.seen() == [("claim", "ran", Status.PENDING), ("failed", "ran", Status.FAILED)]
    failed = log.events[1]
    assert isinstance(failed.error, ValueError)
    assert failed.record is not None and failed.record.status is Status.FAILED


def test_unknown(db: str | Path) -> None:
    log = Log()

    def hang() -> None:
        raise TimeoutError

    with Once(db, hooks=log.hooks()) as once:
        with pytest.raises(TimeoutError):
            once.execute(hang, key="k")
        with pytest.raises(EffectUnknown):
            once.execute(hang, key="k")  # a stored unknown is not a replay
    assert log.seen() == [
        ("claim", "ran", Status.PENDING),
        ("unknown", "unknown", Status.UNKNOWN),
    ]
    assert isinstance(log.events[1].error, TimeoutError)


def test_timeout_and_bad_result_are_unknown(db: str | Path) -> None:
    log = Log()
    with Once(db, hooks=log.hooks()) as once:
        with pytest.raises(EffectUnknown):
            once.execute(lambda: time.sleep(0.5), key="slow", timeout=0.01)
        with pytest.raises(EffectUnknown):
            once.execute(lambda: object(), key="bad")
    unknown = [e for e in log.events if e.kind == "unknown"]
    assert [e.key for e in unknown] == ["slow", "bad"]
    assert all(isinstance(e.error, EffectUnknown) for e in unknown)


def test_expired_rerun(db: str | Path) -> None:
    log = Log()
    with Once(db, hooks=log.hooks()) as once:
        once.execute(charge, key="k", ttl=0.2, amount=1)
        time.sleep(0.35)
        once.execute(charge, key="k", ttl=0.2, amount=1)
    assert [e.outcome for e in log.events] == ["ran", "ran", "expired-rerun", "expired-rerun"]


def test_verify(db: str | Path) -> None:
    log = Log()
    found: dict[str, Any] = {"a": "ch_found"}

    def verify(key: str, args: dict[str, Any]) -> Any:
        return found.get(key, NOT_HAPPENED)

    def hang(amount: int) -> str:
        raise TimeoutError

    with Once(db, hooks=log.hooks()) as once:
        for key in ("a", "b"):
            with pytest.raises(TimeoutError):
                once.execute(hang, key=key, tool_name="charge", amount=1)
        log.events.clear()
        assert once.execute(charge, key="a", verify=verify, amount=1) == "ch_found"
        assert once.execute(charge, key="b", verify=verify, amount=1) == "ch_1"
    assert [(e.key, e.kind, e.outcome) for e in log.events] == [
        ("a", "done", "verified"),
        ("b", "claim", "ran"),
        ("b", "done", "ran"),
    ]
    assert log.events[0].record is not None and log.events[0].record.result == '"ch_found"'


def test_conflict_and_pending_fire_nothing(db: str | Path) -> None:
    log = Log()
    with Once(db, hooks=log.hooks()) as once:
        once.execute(charge, key="k", amount=1)
        log.events.clear()
        with pytest.raises(KeyConflict):
            once.execute(charge, key="k", amount=2)
    assert log.events == []


def test_waited(db: str | Path) -> None:
    log = Log()
    started = threading.Event()

    def slow() -> str:
        started.set()
        time.sleep(0.3)
        return "ok"

    with Once(db) as first, Once(db, wait=5, poll_interval=0.02, hooks=log.hooks()) as second:
        thread = threading.Thread(target=lambda: first.execute(slow, key="k"))
        thread.start()
        started.wait(5)
        assert second.execute(slow, key="k") == "ok"
        thread.join()
    assert log.seen() == [("replay", "waited", Status.DONE)]


def test_hook_errors_are_logged(db: str | Path, caplog: pytest.LogCaptureFixture) -> None:
    def bad(event: Event) -> None:
        raise RuntimeError("hook broke")

    hooks = Hooks(on_claim=bad, on_done=bad, on_replay=bad)
    with Once(db, hooks=hooks) as once, caplog.at_level(logging.ERROR, logger="once"):
        assert once.execute(charge, amount=1) == "ch_1"
        assert once.execute(charge, amount=1) == "ch_1"
    failures = [r for r in caplog.records if "hook failed" in r.getMessage()]
    assert [r.getMessage().split()[1] for r in failures] == ["on_claim", "on_done", "on_replay"]
    assert all(r.exc_info is not None for r in failures)


def test_store_error_while_building_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = Log()
    with Once(tmp_path / "once.db", hooks=log.hooks()) as once:

        def broken(key: str) -> None:
            raise OSError("disk gone")

        monkeypatch.setattr(once.store, "get", broken)
        with caplog.at_level(logging.ERROR, logger="once"):
            assert once.execute(charge, amount=1) == "ch_1"
    assert log.events == []
    assert "could not build the on_claim event" in caplog.text


def test_async_hook_in_sync_execute_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[Event] = []

    async def on_done(event: Event) -> None:
        calls.append(event)

    hooks = Hooks(on_done=on_done)
    with Once(tmp_path / "once.db", hooks=hooks) as once, caplog.at_level(logging.WARNING):
        once.execute(charge, amount=1)
    assert calls == []
    assert "on_done hook is async" in caplog.text


def test_no_hooks_set(tmp_path: Path) -> None:
    with Once(tmp_path / "once.db", hooks=Hooks()) as once:
        assert once.execute(charge, amount=1) == "ch_1"
        assert once.execute(charge, amount=1) == "ch_1"


# async


def test_async_run_then_replay(db: str | Path) -> None:
    log = Log()
    seen_async: list[str] = []

    async def on_replay(event: Event) -> None:
        await asyncio.sleep(0)
        seen_async.append(event.outcome)
        log.events.append(event)

    hooks = Hooks(on_claim=log.events.append, on_done=log.events.append, on_replay=on_replay)

    async def acharge(amount: int) -> str:
        return f"ch_{amount}"

    async def main() -> None:
        with Once(db, hooks=hooks) as once:
            assert await once.aexecute(acharge, amount=5) == "ch_5"
            assert await once.aexecute(acharge, amount=5) == "ch_5"
            assert await once.aexecute(charge, amount=6) == "ch_6"  # plain fn in a thread

    asyncio.run(main())
    assert log.seen() == [
        ("claim", "ran", Status.PENDING),
        ("done", "ran", Status.DONE),
        ("replay", "replayed", Status.DONE),
        ("claim", "ran", Status.PENDING),
        ("done", "ran", Status.DONE),
    ]
    assert seen_async == ["replayed"]
    assert log.events[1].record is not None and log.events[1].record.result == '"ch_5"'


def test_async_outcomes(db: str | Path) -> None:
    log = Log()
    found: dict[str, Any] = {"a": "ch_found"}

    async def verify(key: str, args: dict[str, Any]) -> Any:
        return found.get(key, NOT_HAPPENED)

    async def boom() -> None:
        raise ValueError("no")

    async def hang() -> None:
        raise TimeoutError

    async def slow() -> None:
        await asyncio.sleep(1)

    async def main() -> None:
        with Once(db, hooks=log.hooks()) as once:
            with pytest.raises(ValueError):
                await once.aexecute(boom, key="f")
            with pytest.raises(TimeoutError):
                await once.aexecute(hang, key="a")
            with pytest.raises(EffectUnknown):
                await once.aexecute(slow, key="t", timeout=0.01)
            with pytest.raises(EffectUnknown):
                await once.aexecute(lambda: object(), key="j")
            assert await once.aexecute(hang, key="a", verify=verify) == "ch_found"
            await once.aexecute(charge, key="e", ttl=0.2, amount=1)
            await asyncio.sleep(0.35)
            await once.aexecute(charge, key="e", ttl=0.2, amount=1)

    asyncio.run(main())
    settled = [(e.key, e.kind, e.outcome) for e in log.events if e.kind != "claim"]
    assert settled == [
        ("f", "failed", "ran"),
        ("a", "unknown", "unknown"),
        ("t", "unknown", "unknown"),
        ("j", "unknown", "unknown"),
        ("a", "done", "verified"),
        ("e", "done", "ran"),
        ("e", "done", "expired-rerun"),
    ]
    assert isinstance(log.events[1].error, ValueError)


def test_async_waited(db: str | Path) -> None:
    log = Log()

    async def main() -> None:
        started = asyncio.Event()

        async def slow() -> str:
            started.set()
            await asyncio.sleep(0.3)
            return "ok"

        with Once(db) as first, Once(db, wait=5, poll_interval=0.02, hooks=log.hooks()) as second:
            task = asyncio.create_task(first.aexecute(slow, key="k"))
            await started.wait()
            assert await second.aexecute(slow, key="k") == "ok"
            await task

    asyncio.run(main())
    assert log.seen() == [("replay", "waited", Status.DONE)]


def test_async_waits_on_a_call_in_this_process(tmp_path: Path) -> None:
    # A sync call holds the key; an async caller on another thread waits for it.
    log = Log()
    started = threading.Event()

    def slow() -> str:
        started.set()
        time.sleep(0.3)
        return "ok"

    with Once(tmp_path / "once.db", hooks=log.hooks()) as once:
        thread = threading.Thread(target=lambda: once.execute(slow, key="k"))
        thread.start()
        started.wait(5)
        assert asyncio.run(once.aexecute(slow, key="k")) == "ok"
        thread.join()
    assert log.seen()[-1] == ("replay", "waited", Status.DONE)


def test_async_hook_errors_are_logged(db: str | Path, caplog: pytest.LogCaptureFixture) -> None:
    async def bad_async(event: Event) -> None:
        raise RuntimeError("async hook broke")

    def bad(event: Event) -> None:
        raise RuntimeError("hook broke")

    async def main() -> None:
        with Once(db, hooks=Hooks(on_claim=bad, on_done=bad_async)) as once:
            assert await once.aexecute(charge, amount=1) == "ch_1"

    with caplog.at_level(logging.ERROR, logger="once"):
        asyncio.run(main())
    failures = [
        r.getMessage().split()[1] for r in caplog.records if "hook failed" in r.getMessage()
    ]
    assert failures == ["on_claim", "on_done"]


def test_async_store_error_while_building_an_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = Log()

    async def main() -> None:
        with Once(tmp_path / "once.db", hooks=log.hooks()) as once:

            def broken(key: str) -> None:
                raise OSError("disk gone")

            monkeypatch.setattr(once.store, "get", broken)
            assert await once.aexecute(charge, amount=1) == "ch_1"

    with caplog.at_level(logging.ERROR, logger="once"):
        asyncio.run(main())
    assert log.events == []
    assert "could not build the on_claim event" in caplog.text

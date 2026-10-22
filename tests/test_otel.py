"""OpenTelemetry spans, checked with the SDK's in-memory exporter."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from once import NOT_HAPPENED, EffectFailed, EffectPending, KeyConflict, Once, _otel

_exporter = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider() -> None:
    # The global provider can only be set once per process. Build it with the
    # SDK enabled even if OTEL_SDK_DISABLED is set in the environment.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        disabled = os.environ.pop("OTEL_SDK_DISABLED", None)
        try:
            tp = TracerProvider()
        finally:
            if disabled is not None:
                os.environ["OTEL_SDK_DISABLED"] = disabled
        tp.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(tp)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    _exporter.clear()
    yield _exporter
    _exporter.clear()


def once_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("once ")]


def summary(exporter: InMemorySpanExporter) -> list[tuple[str, Any, Any]]:
    return [
        (s.name, s.attributes.get("once.outcome"), s.attributes.get("once.status"))
        for s in once_spans(exporter)
        if s.attributes is not None
    ]


def charge(amount: int) -> str:
    return f"ch_{amount}"


def test_ran_then_replayed(db: str | Path, spans: InMemorySpanExporter) -> None:
    with Once(db) as once:
        once.execute(charge, key="k", amount=1)
        once.execute(charge, key="k", amount=1)
    assert summary(spans) == [
        ("once charge", "ran", "done"),
        ("once charge", "replayed", "done"),
    ]
    first = once_spans(spans)[0]
    assert first.attributes is not None
    assert first.attributes["once.key"] == "k" and first.attributes["once.tool"] == "charge"
    assert first.status.status_code is StatusCode.UNSET


def test_failure_is_recorded(db: str | Path, spans: InMemorySpanExporter) -> None:
    def boom() -> None:
        raise ValueError("card declined")

    with Once(db) as once:
        with pytest.raises(ValueError):
            once.execute(boom, key="k")
        with pytest.raises(EffectFailed):
            once.execute(boom, key="k")
    assert summary(spans) == [("once boom", "ran", "failed"), ("once boom", "replayed", "failed")]
    first, second = once_spans(spans)
    assert first.status.status_code is StatusCode.ERROR
    assert [e.name for e in first.events] == ["exception"]
    assert first.events[0].attributes is not None
    assert first.events[0].attributes["exception.type"] == "ValueError"
    assert second.events[0].attributes is not None
    assert second.events[0].attributes["exception.type"] == "once.exceptions.EffectFailed"


def test_unknown_verified_and_expired(db: str | Path, spans: InMemorySpanExporter) -> None:
    def hang(amount: int) -> str:
        raise TimeoutError

    def verify(key: str, args: dict[str, Any]) -> Any:
        return "ch_found" if key == "a" else NOT_HAPPENED

    with Once(db) as once:
        for key in ("a", "b"):
            with pytest.raises(TimeoutError):
                once.execute(hang, key=key, tool_name="charge", amount=1)
        once.execute(charge, key="a", verify=verify, amount=1)
        once.execute(charge, key="b", verify=verify, amount=1)
        once.execute(charge, key="e", ttl=0.2, amount=1)
        time.sleep(0.35)
        once.execute(charge, key="e", ttl=0.2, amount=1)
    assert summary(spans) == [
        ("once charge", "unknown", "unknown"),
        ("once charge", "unknown", "unknown"),
        ("once charge", "verified", "done"),
        ("once charge", "ran", "done"),
        ("once charge", "ran", "done"),
        ("once charge", "expired-rerun", "done"),
    ]


def test_conflict_and_pending(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    path = tmp_path / "once.db"
    started, release = threading.Event(), threading.Event()

    def slow(amount: int) -> str:
        started.set()
        release.wait(5)
        return "ok"

    with Once(path) as first, Once(path) as second:
        first.execute(charge, key="c", amount=1)
        with pytest.raises(KeyConflict):
            first.execute(charge, key="c", amount=2)
        thread = threading.Thread(target=lambda: first.execute(slow, key="p", amount=1))
        thread.start()
        started.wait(5)
        with pytest.raises(EffectPending):
            second.execute(slow, key="p", amount=1)
        release.set()
        thread.join()
    outcomes = [(s.attributes or {}).get("once.outcome") for s in once_spans(spans)]
    assert outcomes == ["ran", "conflict", "pending", "ran"]


def test_waited(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    path = tmp_path / "once.db"
    started = threading.Event()

    def slow() -> str:
        started.set()
        time.sleep(0.3)
        return "ok"

    with Once(path) as first, Once(path, wait=5, poll_interval=0.02) as second:
        thread = threading.Thread(target=lambda: first.execute(slow, key="k"))
        thread.start()
        started.wait(5)
        second.execute(slow, key="k")
        thread.join()
    assert sorted(summary(spans)) == [("once slow", "ran", "done"), ("once slow", "waited", "done")]


def test_span_is_a_child_of_the_current_span(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    tracer = trace.get_tracer("test")

    def inner() -> str:
        with tracer.start_as_current_span("inside"):
            return "x"

    with tracer.start_as_current_span("agent step") as parent, Once(tmp_path / "o.db") as once:
        once.execute(inner)
    by_name = {s.name: s for s in spans.get_finished_spans()}
    assert by_name["once inner"].parent is not None
    assert by_name["once inner"].parent.span_id == parent.get_span_context().span_id
    inside = by_name["inside"].parent
    assert inside is not None and inside.span_id == by_name["once inner"].context.span_id


def test_async(db: str | Path, spans: InMemorySpanExporter) -> None:
    async def acharge(amount: int) -> str:
        return f"ch_{amount}"

    async def boom() -> None:
        raise ValueError("no")

    async def main() -> None:
        with Once(db) as once:
            await once.aexecute(acharge, key="k", amount=1)
            await once.aexecute(acharge, key="k", amount=1)
            with pytest.raises(ValueError):
                await once.aexecute(boom, key="f")

    asyncio.run(main())
    assert summary(spans) == [
        ("once acharge", "ran", "done"),
        ("once acharge", "replayed", "done"),
        ("once boom", "ran", "failed"),
    ]
    assert once_spans(spans)[2].status.status_code is StatusCode.ERROR


def test_importing_once_does_not_import_opentelemetry() -> None:
    code = (
        "import sys, once\n"
        "assert not [m for m in sys.modules if m.startswith('opentelemetry')]\n"
        "once.Once(':memory:').execute(lambda: 1, key='k')\n"
        "assert 'opentelemetry' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_without_opentelemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setattr(_otel, "_tracer", _otel._UNSET)
    assert _otel.tracer() is None
    with Once(tmp_path / "once.db") as once:
        assert once.execute(charge, amount=1) == "ch_1"

        async def main() -> Any:
            return await once.aexecute(charge, amount=1)

        assert asyncio.run(main()) == "ch_1"

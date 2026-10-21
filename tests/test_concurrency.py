from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from once import EffectPending, Once, Status, args_hash


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "once.db"


@pytest.fixture
def once(db: Path) -> Once:
    return Once(db)


SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_threads_with_same_key_run_once(once: Once) -> None:
    calls = 0
    lock = threading.Lock()
    start = threading.Barrier(16)
    results: list[object] = []

    @once.tool
    def charge(amount: int) -> str:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)
        return "ch_1"

    def worker() -> None:
        start.wait()
        results.append(charge(100))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == 1
    assert results == ["ch_1"] * 16


def test_threads_with_separate_instances_run_once(db: Path) -> None:
    calls = 0
    lock = threading.Lock()
    start = threading.Barrier(8)
    outcomes: list[str] = []

    def charge() -> str:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.2)
        return "ch_1"

    def worker() -> None:
        o = Once(db, wait=2.0)
        start.wait()
        outcomes.append(o.execute(charge, key="k"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == 1
    assert outcomes == ["ch_1"] * 8


WORKER = """
import sys, time
sys.path.insert(0, {src!r})
from once import Once, EffectPending

o = Once({db!r})
while time.time() < {start}:
    time.sleep(0.001)

def charge():
    with open({log!r}, "a") as f:
        f.write("x")
    time.sleep(0.3)
    return "ch_1"

try:
    print(o.execute(charge, key="k"))
except EffectPending:
    print("pending")
"""


def test_processes_with_same_key_run_once(tmp_path: Path) -> None:
    db = tmp_path / "once.db"
    log = tmp_path / "calls.log"
    Once(db)  # create the schema up front
    script = WORKER.format(src=SRC, db=str(db), log=str(log), start=time.time() + 1.0)
    procs = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    outputs = [p.communicate(timeout=30)[0].strip() for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert log.read_text() == "x"
    assert outputs.count("ch_1") == 1
    assert outputs.count("pending") == 5


def test_pending_raises_without_wait(db: Path) -> None:
    def f() -> int:
        return 1

    other = Once(db)
    assert other.store.claim("k", args_hash({"tool": "f", "args": {}}))
    with pytest.raises(EffectPending) as info:
        Once(db).execute(f, key="k")
    assert info.value.record.status is Status.PENDING


def test_wait_returns_result_once_pending_finishes(db: Path) -> None:
    h = args_hash({"tool": "f", "args": {}})
    owner = Once(db)
    token = owner.store.claim("k", h)
    assert token is not None

    def finish_later() -> None:
        time.sleep(0.2)
        owner.store.finish("k", token, Status.DONE, result='"late"')

    threading.Thread(target=finish_later).start()

    def f() -> str:
        raise AssertionError("should not run")

    assert Once(db, wait=3.0).execute(f, key="k") == "late"

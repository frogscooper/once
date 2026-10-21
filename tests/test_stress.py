"""Many processes, threads and coroutines on one key: exactly one execution."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from once import Once

SRC = str(Path(__file__).resolve().parents[1] / "src")

WORKER = """
import asyncio, os, sys, threading, time
sys.path.insert(0, {src!r})
from once import Once

once = Once({db!r}, wait=60, poll_interval=0.01)
results = []

def effect():
    fd = os.open({log!r}, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    os.write(fd, b"x")
    os.close(fd)
    time.sleep(0.2)
    return "ch_1"

def sync_charge(amount):
    return effect()

async def async_charge(amount):
    return await asyncio.to_thread(effect)

async def coroutines():
    return await asyncio.gather(*(
        once.aexecute(async_charge, key="k", tool_name="charge", amount=1)
        for _ in range({coros})
    ))

def thread_main(i):
    if i % 2:
        results.extend(asyncio.run(coroutines()))
    else:
        for _ in range({coros}):
            results.append(once.execute(sync_charge, key="k", tool_name="charge", amount=1))

while time.time() < {start}:
    time.sleep(0.001)
threads = [threading.Thread(target=thread_main, args=(i,)) for i in range({threads})]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert results == ["ch_1"] * len(results), results
print(len(results))
"""


def run_stress(tmp_path: Path, procs: int, threads: int, coros: int) -> None:
    db = tmp_path / "once.db"
    log = tmp_path / "effects.log"
    Once(db).close()
    script = WORKER.format(
        src=SRC,
        db=str(db),
        log=str(log),
        coros=coros,
        threads=threads,
        start=time.time() + 2 + procs * 0.2,
    )
    workers = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(procs)
    ]
    outputs = [w.communicate(timeout=120)[0] for w in workers]
    assert [w.returncode for w in workers] == [0] * procs
    assert sum(int(o) for o in outputs) == procs * threads * coros
    assert log.read_bytes() == b"x"


def test_stress(tmp_path: Path) -> None:
    run_stress(tmp_path, procs=4, threads=4, coros=5)


@pytest.mark.heavy
def test_stress_heavy(tmp_path: Path) -> None:
    run_stress(tmp_path, procs=12, threads=8, coros=25)

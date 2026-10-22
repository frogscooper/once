from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from once import Once, Status
from once.cli import main, parse_age


@pytest.fixture
def db(db: str | Path) -> str | Path:
    """The shared db fixture with three records in it."""
    path = db
    with Once(path) as once:
        once.execute(lambda: {"id": "ch_1"}, key="aaa111")
        once.execute(lambda: "ok", key="aaa222")
        with pytest.raises(ValueError):
            once.execute(_fail, key="bbb333")
    time.sleep(0.05)  # so "older than 0s" is true even with a coarse clock
    return path


def _fail() -> None:
    raise ValueError("declined")


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_ls(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "ls", "--db", str(db))
    assert code == 0
    rows = [line.split("\t") for line in out.splitlines()]
    assert sorted(r[0] for r in rows) == ["aaa111", "aaa222", "bbb333"]
    failed = next(r for r in rows if r[0] == "bbb333")
    assert failed[1] == "failed" and "declined" in failed[3]

    code, out, _ = run(capsys, "ls", "--db", str(db), "--status", "failed")
    assert [line.split("\t")[0] for line in out.splitlines()] == ["bbb333"]

    code, out, _ = run(capsys, "ls", "--db", str(db), "--limit", "1")
    assert len(out.splitlines()) == 1


def test_db_from_env(
    db: str | Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONCE_DB", str(db))
    code, out, _ = run(capsys, "ls")
    assert code == 0 and len(out.splitlines()) == 3


def test_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "ls", "--db", str(tmp_path / "nope.db"))
    assert code == 1 and "no database" in err
    assert not (tmp_path / "nope.db").exists()


def test_show(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "show", "--db", str(db), "aaa111")
    assert code == 0
    data = json.loads(out)
    assert data["status"] == "done" and data["result"] == {"id": "ch_1"}

    code, out, _ = run(capsys, "show", "--db", str(db), "bbb")  # unique prefix
    assert json.loads(out)["error"] == "ValueError: declined"

    code, _, err = run(capsys, "show", "--db", str(db), "aaa")
    assert code == 1 and "more than one" in err
    code, _, err = run(capsys, "show", "--db", str(db), "zzz")
    assert code == 1 and "no record" in err


def test_reset(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "reset", "--db", str(db), "bbb3")
    assert code == 0 and out.strip() == "deleted bbb333"
    with Once(db) as once:
        assert once.get("bbb333") is None
    code, _, _ = run(capsys, "reset", "--db", str(db), "bbb333")
    assert code == 1


def test_gc(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "gc", "--db", str(db), "--older-than", "1h")
    assert out.strip() == "deleted 0 records"
    code, out, _ = run(capsys, "gc", "--db", str(db), "--older-than", "0")
    assert code == 0 and out.strip() == "deleted 2 records"  # done only by default
    code, out, _ = run(capsys, "gc", "--db", str(db), "--older-than", "0s", "--status", "all")
    assert out.strip() == "deleted 1 record"


def test_gc_status_filter(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "gc", "--db", str(db), "--older-than", "0", "--status", "failed")
    with Once(db) as once:
        assert [r.key for r in once.records(status=Status.FAILED)] == []
        assert len(once.records()) == 2


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90", 90),
        ("30s", 30),
        ("15m", 900),
        ("12h", 43200),
        ("7d", 604800),
        ("2w", 1209600),
        ("1.5h", 5400),
    ],
)
def test_parse_age(text: str, seconds: float) -> None:
    assert parse_age(text) == seconds


def test_bad_age(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["gc", "--db", str(db), "--older-than", "soon"])
    assert "bad age" in capsys.readouterr().err


def test_python_dash_m(db: str | Path) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "once", "ls", "--db", str(db), "--status", "done"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(out.splitlines()) == 2

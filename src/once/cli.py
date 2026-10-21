"""Command line tool for inspecting and cleaning up a once database."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .store import Record, SQLiteStore, Status

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_STATUSES = [s.value for s in Status]


class _UsageError(Exception):
    pass


def parse_age(text: str) -> float:
    """Parse ``90``, ``30s``, ``15m``, ``12h``, ``7d`` or ``2w`` into seconds."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw]?)", text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"bad age {text!r}, use e.g. 30s, 15m, 12h, 7d")
    return float(match.group(1)) * _UNITS[match.group(2) or "s"]


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=os.environ.get("ONCE_DB", "once.db"),
        help="SQLite file (default: $ONCE_DB or ./once.db)",
    )
    parser = argparse.ArgumentParser(prog="once", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("ls", parents=[common], help="list records")
    ls.add_argument("--status", action="append", choices=_STATUSES, help="filter, repeatable")
    ls.add_argument("--limit", type=int, default=None)

    show = sub.add_parser("show", parents=[common], help="show one record")
    show.add_argument("key", help="key or a unique prefix of it")

    reset = sub.add_parser("reset", parents=[common], help="delete a record so it can run again")
    reset.add_argument("key", help="key or a unique prefix of it")

    gc = sub.add_parser("gc", parents=[common], help="delete old records")
    gc.add_argument("--older-than", type=parse_age, required=True, metavar="AGE")
    gc.add_argument(
        "--status",
        action="append",
        choices=[*_STATUSES, "all"],
        help="statuses to delete, repeatable (default: done)",
    )
    return parser


def _open(path: str) -> SQLiteStore:
    if not Path(path).is_file():
        raise _UsageError(f"no database at {path}")
    return SQLiteStore(path)


def _resolve(store: SQLiteStore, key: str) -> str:
    if store.get(key) is not None:
        return key
    matches = store.keys_with_prefix(key)
    if not matches:
        raise _UsageError(f"no record for {key}")
    if len(matches) > 1:
        raise _UsageError(f"{key} matches more than one key, use more characters")
    return matches[0]


def _show(record: Record) -> str:
    result = json.loads(record.result) if record.result is not None else None
    data = {
        "key": record.key,
        "status": record.status.value,
        "result": result,
        "error": record.error,
        "args_hash": record.args_hash,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    return json.dumps(data, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = _open(args.db)
    except _UsageError as exc:
        print(f"once: {exc}", file=sys.stderr)
        return 1
    try:
        if args.command == "ls":
            statuses = [Status(s) for s in args.status] if args.status else None
            for r in store.records(statuses, args.limit):
                print("\t".join([r.key, r.status.value, r.updated_at, r.error or ""]))
        elif args.command == "show":
            record = store.get(_resolve(store, args.key))
            assert record is not None
            print(_show(record))
        elif args.command == "reset":
            key = _resolve(store, args.key)
            store.delete(key)
            print(f"deleted {key}")
        else:
            chosen = args.status or ["done"]
            statuses = list(Status) if "all" in chosen else [Status(s) for s in chosen]
            count = store.purge(args.older_than, statuses)
            print(f"deleted {count} record{'' if count == 1 else 's'}")
    except _UsageError as exc:
        print(f"once: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0

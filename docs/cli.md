# CLI

Installing the package adds a `once` command (`python -m once` works too) for looking at and cleaning up the database.

```
once ls                          # most recent first: key, status, updated_at, error
once ls --status unknown         # repeat --status to show several
once ls --limit 20
once show 3f9a                   # one record as JSON; a unique key prefix is enough
once reset 3f9a                  # delete it so the next call runs again
once gc --expired                # delete done records whose ttl has passed
once gc --older-than 7d          # delete done records not updated for 7 days
once gc --older-than 30d --status failed --status done
once gc --older-than 30d --status all
```

## Options

Every command takes:

- `--db`: a SQLite file or a `postgresql://` URL. Defaults to `$ONCE_DB`, else `once.db`.
- `--table`: the Postgres table. Defaults to `$ONCE_TABLE`, else `once_effects`. Ignored for SQLite.

The CLI never creates anything. A missing SQLite file or Postgres table is an error (exit code 1).

## gc

`gc` needs `--older-than`, `--expired`, or both.

- `--older-than AGE` deletes records not updated for that long. AGE is seconds (`90`) or a number with `s`, `m`, `h`, `d` or `w` (`30s`, `15m`, `12h`, `7d`, `2w`).
- `--status` picks which statuses `--older-than` deletes. It defaults to `done` and can be repeated; `all` means every status.
- `--expired` deletes done records past their ttl. See [Expiry and gc](ttl.md).

Deleting pending or unknown records lets those calls run again, so pick statuses on purpose.

`show` and `reset` match a unique key prefix, and fail if the prefix matches no key or more than one.

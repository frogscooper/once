# Expiry and gc

By default a done result is kept forever. Give it a time to live when the effect should happen again later, like a daily digest or a cache refresh.

## Setting a ttl

`ttl` is seconds (int or float) or a `timedelta`. It can be set in four places:

```python
from datetime import timedelta

from once import Once

once = Once("once.db", ttl=timedelta(days=30))  # default for every call


@once.tool(ttl=3600)  # this tool: one hour
def send_digest(user_id: str) -> str: ...


once.execute(send_digest, user_id="u_1", ttl=60)  # this call: one minute
```

`await once.aexecute(..., ttl=...)` takes it too. The call's value beats the tool's, which beats the `Once` default. `math.inf` means never. Zero, negative and NaN values raise `ValueError`.

The clock starts when the call finishes, and the record keeps the deadline in `expires_at` (an ISO 8601 UTC string). A result stored by `verify` gets an `expires_at` the same way.

## What expiry means

Only done records expire. Once a done record is past `expires_at`, it counts as gone:

- the next call with the key runs again, even with different arguments (no `KeyConflict`).
- if several callers get there at once, only one of them runs it. Taking over the expired record is a conditional update, so exactly one caller wins. The rest see a pending record and wait for its result like any other pending call.

Failed and unknown records never expire. They keep blocking until you `reset` them or `verify` resolves them, since someone has to decide whether the effect happened.

## Deleting expired records

Expired records stay in the table until you delete them:

```
once gc --expired
```

or from Python:

```python
once.purge(expired=True)
```

Run it on a schedule, for example from cron:

```
0 3 * * * once gc --expired --db /srv/app/once.db
```

`purge` also takes `older_than` (seconds since the last update) and `status` (done by default). Pass either or both:

```python
once.purge(older_than=7 * 86400)  # done records not updated for a week
once.purge(older_than=30 * 86400, status=["done", "failed"], expired=True)
```

Purging pending or unknown records lets those calls run again, so only do that on purpose.

## Old databases

SQLite databases and Postgres tables made before ttl support get the `expires_at` column and its index added when they are opened. Their old records never expire. With `PostgresStore(create=False)`, the database user still needs the right to alter the table and create the index once, or you can add them yourself.

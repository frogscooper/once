# once

`once` makes side-effecting tool calls (charge a card, send an email, create a ticket) safe for an AI agent to retry. Each distinct call runs at most once; a replay gets the stored result instead of running the side effect again.

Agents retry after timeouts, crash and resume, or re-plan and call the same tool twice. `once` records every call in SQLite under an idempotency key so the duplicates do nothing.

## Install

```
pip install agent-once
```

The import name is `once`. Python 3.10+, no dependencies.

## Quickstart

```python
from once import Once, make_key

once = Once()  # SQLite in ./once.db, or Once(path="...")


@once.tool(name="charge_card")
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict:
    charge = stripe.Charge.create(
        amount=amount,
        currency="usd",
        customer=customer,
        idempotency_key=idempotency_key,
    )
    return {"id": charge.id}


with once.scope("order_456"):
    charge_card(1000, "cus_123")  # charges
    charge_card(1000, "cus_123")  # returns the stored {"id": ...}, no second charge
```

Or call it explicitly with your own key:

```python
key = make_key("charge_card", {"amount": 1000, "customer": "cus_123"}, scope="order_456")
result = once.execute(fn=charge_card, key=key, amount=1000, customer="cus_123")
```

Register the decorated function with your agent framework as a normal tool. See `examples/stripe_example.py`.

## Keys

The key is `sha256(canonical_json({"tool": name, "args": args, "scope": scope}))`. Arguments are bound to the function signature with defaults filled in, so `charge_card(1000, "cus_123")` and `charge_card(amount=1000, customer="cus_123")` get the same key. Arguments must be JSON serializable.

The scope is usually a run, order, or step id. Set it with `with once.scope(...)` or pass `scope=` to `execute`. Without a scope, the same call with the same arguments only ever runs once, which is often not what you want for things like "send a reminder".

If the function has an `idempotency_key` parameter, `once` passes the key in, so you can forward it to an API that supports idempotency keys. If the caller passes `idempotency_key` itself, that value becomes the key.

Each record also stores a hash of the tool name and arguments. Reusing a key with different arguments raises `KeyConflict`.

## States

| status    | meaning                                  | on replay              |
|-----------|------------------------------------------|------------------------|
| `pending` | claimed, running now (or the owner died) | raise `EffectPending`  |
| `done`    | finished, result stored as JSON          | return the result      |
| `failed`  | raised a known error                     | raise `EffectFailed`   |
| `unknown` | timed out or was interrupted mid-call    | raise `EffectUnknown`  |

The first call re-raises the original exception; later calls raise the `Effect*` error, which has the stored `.record`. Failed and unknown records are never retried automatically. Once you have checked what happened, call `once.reset(key)` to allow another attempt.

`TimeoutError` marks a record unknown, since the request may have reached the other side. Pass `unknown_on=(TimeoutError, MyClientError)` to change that. `execute(..., timeout=5)` and `@once.tool(timeout=5)` stop waiting after 5 seconds and mark the record unknown. The call keeps running in a background thread since Python cannot kill it; its outcome is discarded.

`Once(wait=2.0)` waits up to 2 seconds for a pending record owned by another process before raising `EffectPending`. Calls in the same process with the same key are serialized by a lock, so they simply get the result.

Return values go through JSON, so the first call and replays return the same thing (a tuple comes back as a list). If the result cannot be serialized, the record is marked unknown.

## Storage

One table in SQLite, in WAL mode with a busy timeout:

```sql
CREATE TABLE effects (key TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT,
  error TEXT, args_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
```

A call claims its key with `INSERT ... ON CONFLICT DO NOTHING`. Whoever inserts the row runs the function. This works across threads and processes on one machine. SQLite over a network filesystem is not supported.

## Limitations

This is at-most-once, not exactly-once. If the process dies after the side effect happens but before the result is written, the record stays `pending` forever and the call will not run again. That is on purpose: running it twice is the thing we are trying to avoid. You need to look at the downstream system and then `reset` the key or leave it.

The way to close that gap is to forward the key to the downstream API's own idempotency key (Stripe, many payment and messaging APIs support one). Then a retry after `reset` is safe too.

Async functions are not supported yet.

## License

MIT

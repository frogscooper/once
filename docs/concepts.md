# Concepts

## Statuses

Every call gets a record under its key. The record has one of four statuses:

| status | meaning | a later call with the key |
|--------|---------|---------------------------|
| `pending` | claimed and running, or the owner died | raises `EffectPending` |
| `done` | finished, result stored as JSON | returns the stored result |
| `failed` | raised a known error | raises `EffectFailed` |
| `unknown` | timed out, was interrupted, or the result could not be stored | raises `EffectUnknown` |

A call claims its key with `INSERT ... ON CONFLICT DO NOTHING`. Whoever inserts the row runs the function. Everyone else reads the record.

The first call re-raises the original exception. Later calls raise the `Effect*` error, which has the stored `.record` and `.key`. All of them inherit from `OnceError`, as does `KeyConflict`.

Failed and unknown records are never retried on their own. Once you know what happened, `once.reset(key)` deletes the record so the next call runs again.

Return values go through JSON, so the first call and every replay return the same thing (a tuple comes back as a list). Arguments must be JSON serializable too, or the call raises `TypeError` before anything runs.

## Unknown

`unknown` means the effect may or may not have happened. A record becomes unknown when:

- the function raises one of the `unknown_on` exception types. The default is `(TimeoutError,)`, since a timed out request may have reached the other side. Change it with `Once(unknown_on=(TimeoutError, MyClientError))`.
- the function raises something that is not an `Exception`, like `KeyboardInterrupt` or `asyncio.CancelledError`.
- `timeout=` runs out. `execute(..., timeout=5)` and `@once.tool(timeout=5)` stop waiting after 5 seconds. A sync call keeps running in a daemon thread, since Python cannot kill it, and its outcome is discarded. An async call is cancelled with `asyncio.wait_for`.
- the result is not JSON serializable.

Any other exception marks the record `failed`.

## Pending and wait

A pending record owned by another process raises `EffectPending` right away. `Once(wait=2.0)` waits up to 2 seconds for it to finish first, polling every `poll_interval` seconds (default 0.05).

Calls in the same process wait for each other without that setting: threads with the same key are serialized by a lock, and coroutines on one event loop by an `asyncio.Lock`. A sync and an async caller, or callers on two loops, also wait for whoever started first. One exception: a blocking sync call on an event loop's thread, for a key a coroutine on that same loop is running, raises `EffectPending` instead of deadlocking.

## Verify

An unknown record stays stuck until someone checks the downstream system. If that system can answer the question, give the tool a `verify` callback and `once` asks it on the next call instead of raising `EffectUnknown`:

```python
from once import NOT_HAPPENED


def find_charge(key: str, args: dict) -> object:
    charges = stripe.Charge.search(query=f"metadata['once_key']:'{key}'")
    if not charges.data:
        return NOT_HAPPENED
    return {"id": charges.data[0].id}


@once.tool(name="charge_card", verify=find_charge)
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict:
    charge = stripe.Charge.create(
        amount=amount,
        currency="usd",
        customer=customer,
        idempotency_key=idempotency_key,
        metadata={"once_key": idempotency_key},
    )
    return {"id": charge.id}
```

`verify(key, args)` gets the key and the call arguments (without excluded ones). Then:

- it returns a result: the record becomes `done` with that result, and nothing runs.
- it returns `NOT_HAPPENED`: the record is taken over and the function runs again.
- it raises: the record stays as it was and the error propagates.

`execute(..., verify=...)` works the same way. For async tools and `aexecute`, `verify` may be async. Failed records are never verified. [Verifiers](verifiers.md) has ready-made callbacks for Stripe and HTTP APIs.

## stale_after

A process that dies mid-call leaves its record `pending` forever. `Once(stale_after=600)` treats a pending record older than 600 seconds as abandoned and hands it to `verify`, if the call has one. Without `verify`, nothing changes: `once` never re-runs a call unless a callback says the effect did not happen.

!!! warning "stale_after can run a call twice"
    A pending record that is merely slow looks the same as a dead one. If the call is still running when its record turns stale, `verify` may not see the effect yet, say it did not happen, and the call runs again while the first run is still going. Pick a value well above the tool's longest run, and prefer a `verify` backed by the downstream idempotency key.

Pick a value well above the tool's longest run. A record that was taken over can no longer be written by its old owner: each owner holds a token, and the store only accepts the outcome while the token still matches.

With Postgres and workers on several hosts, the record's age comes from each worker's clock, so keep `stale_after` well above any clock drift too.

## Scope

The scope separates one run, order or step from the next. Set it for a block of code:

```python
with once.scope("order_456"):
    charge_card(1000, "cus_123")
```

or per call with `execute(..., scope="order_456")`. Scopes are context variables, so they follow async tasks and nest: the innermost `once.scope()` wins, and `current_scope()` (from `once`) returns it, or None. The framework [integrations](integrations/index.md) set the scope from the framework's own run or thread id.

`None` and `""` are different scopes.

## Keys

The key is

```
sha256(canonical_json([tool_name, args, scope]))
```

as hex, where `canonical_json` is `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`. The three parts go in one JSON array, so no choice of name, arguments or scope can collide with another. `make_key(tool_name, args, scope=None)` (from `once`) computes it.

Arguments are bound to the function signature with defaults filled in, so `charge_card(1000, "cus_123")` and `charge_card(amount=1000, customer="cus_123")` get the same key. `@once.tool(exclude=("ctx",))` leaves parameters out of the key; they are still passed to the function.

The tool name defaults to the function's `__name__`. Pass `name=` to keep keys stable if you rename the function.

If the function has an `idempotency_key` parameter:

- `once` passes the key in, so you can forward it downstream.
- If the caller passes a non-empty `idempotency_key`, that value is the key. `""` and `None` mean "compute one".

Each record also stores a hash of the tool name and arguments. Reusing a key with different arguments raises `KeyConflict`, unless the old record has [expired](ttl.md).

## Picking the identity with `key=`

By default every argument is part of the key. If a retry or replay re-plans the call with slightly different arguments (a new memo, a rounded amount), the key changes and the side effect runs again. Pass `key=` to say what makes two calls the same:

```python
@once.tool(key=lambda a: a["order_id"])
def refund(order_id: str, amount: int, memo: str = "") -> str:
    return stripe.Refund.create(...).id


refund("o1", 500, memo="damaged")  # runs
refund("o1", 450, memo="damaged item")  # same order_id: returns the first result
```

`key` gets a dict of the bound arguments, with defaults filled in and `idempotency_key` left out. It returns a string or any other JSON value. The key is then `make_key(tool_name, {"key": value}, scope)`, so two tools with the same order id don't collide, and the stored hash covers only that value, so different arguments replay instead of raising `KeyConflict`. A non-empty `idempotency_key` from the caller still wins. If `key` raises or returns `None` or an empty value, the call fails before anything is claimed. `key` and `exclude` can't be used together. The framework helpers take `key=` too.

## Gotchas

- **Keys follow the JSON.** `10` and `10.0` give different keys. A tuple and a list with the same items give the same key, and so do `{1: "a"}` and `{"1": "a"}`, since JSON object keys are strings; a dict that mixes key types can't be serialized at all. Adding a parameter with a default changes every key for that tool, because defaults are part of the key, so old records stop matching. Leave the new parameter out with `exclude=`, or pass `key=` yourself.
- **`execute` takes some names for itself.** `key`, `tool_name`, `scope`, `timeout`, `verify` and `ttl` go to `execute`, not to your function. If your function has a parameter with one of those names, use `@once.tool`, which passes all arguments through.
- **Sync calls block the event loop.** A sync `execute` or sync tool called from async code holds the loop for the whole call, store access included. Use `aexecute` or an `async def` tool there. On the loop's thread, a sync call for a key a coroutine on that loop is running raises `EffectPending`.
- **Calling the same key from inside its own call** raises `OnceError` instead of waiting on itself forever. Use a different key for the inner call.
- **`close()` is not the end.** It closes the store's connections, including the SQLite connections other threads opened, and the next call opens new ones. Don't call it while other threads are mid-call; close once, at shutdown.
- **MCP: pass `scope_arg`.** The default scope is the MCP session id, and a client that reconnects gets a new one, so a retry after a reconnect runs again. Name an argument that identifies the effect, like `scope_arg="order_id"`.
- **`stale_after` can run a call twice.** See the warning [above](#stale_after).
- **Some Stripe statuses stay unknown.** `stripe_verifier` raises on a PaymentIntent that is `processing` or waiting on the customer, so the record stays `unknown` until you look and `reset` it. See [verifiers](verifiers.md).

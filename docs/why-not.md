# Why not just X

## Stripe idempotency keys

If the only side effect is a Stripe call, Stripe's `Idempotency-Key` already stops a double charge, and you may not need `once`.

`once` covers the rest:

- Most APIs an agent calls have no idempotency key at all: email, Slack, ticket systems, your own services.
- Something still has to produce the same key for the same intent across retries, restarts and re-plans. `once` derives it from the tool name, arguments and scope.
- Stripe keeps keys for at least 24 hours. A `once` record lasts until you delete it or its ttl ends.
- `once` returns the stored result without calling out at all, and keeps a record you can list and inspect.

They work well together: pass the `once` key to Stripe, as in [Verifiers](verifiers.md#stripe).

## Temporal or Restate

Durable execution engines record each step and replay a workflow from its history after a crash. If you already run one, use it.

They are a bigger commitment: a server or service to operate, and code written as workflows and activities under their rules. Activities run at least once and are retried on failure or timeout, so an activity that sends an email still needs its own idempotency, which is the problem `once` solves.

`once` is a library and a table. It wraps the tool functions you already have, in whatever agent framework you use, and does nothing else. It doesn't schedule, retry or resume work.

## A Redis lock

A lock keeps two calls from running at the same time. It does not remember that the first one finished, so a retry ten minutes later runs the effect again. To fix that you store the result, which is most of what `once` does.

There are also the edge cases:

- A lock with a timeout can expire while the call is still running, and a second call starts.
- If the process dies mid-call you can't tell whether the effect happened. `once` leaves the record `pending`, which blocks retries. With `stale_after` it goes to `verify`, and nothing runs again unless `verify` or a person says the effect didn't happen.
- Reusing a key with different arguments should be an error, not a silent replay.

`once` uses the database you probably have already (SQLite or Postgres), not a new service.

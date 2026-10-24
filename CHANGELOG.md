# Changelog

## 0.1.0 (unreleased)

First release.

- `Once` with `@once.tool`, `execute` and `aexecute`, backed by SQLite.
- Records move through pending, done, failed and unknown. Failed and unknown are never re-run on their own.
- `verify` callbacks and `stale_after` to resolve unknown and abandoned pending records.
- Async tools, `timeout=`, cancellation marks unknown. Sync and async callers in one process wait for each other.
- `once` command line tool: `ls`, `show`, `reset`, `gc`. `Once.records()` and `Once.purge()`.
- `Once.close()`, and `Once` works as a context manager.
- `exclude=` on `once.tool` to leave parameters out of the key.
- `once.integrations.mcp` and `once.integrations.langchain` decorators, with examples.
- `once.integrations.openai_agents`, `once.integrations.pydantic_ai` and `once.integrations.crewai` decorators, with examples and extras. The `crewai` and `openai-agents` extras can't be installed together. Pydantic AI tools take their scope from `scope_arg`, then `scope_from(ctx)`, then the enclosing `once.scope()` (read with `current_scope()`), then `ctx.conversation_id`, or `ctx.run_id` with `scope="run"`.
- The framework helpers take `ttl=`.
- `PostgresStore` for workers on several machines, behind the `postgres` extra. Use `Once("postgresql://...")` or `Once(store=PostgresStore(dsn, table=...))`. The CLI accepts a Postgres URL for `--db` and has a `--table` option.
- `Store` protocol, so `Once(store=...)` takes any backend that implements it.
- `once.verifiers`: `stripe_verifier` and `stripe_params` for Stripe charges, PaymentIntents and refunds (`stripe` extra, or pass a client), and `http_verifier` for any API that can look an effect up by key. `stripe_verifier` falls back to listing when search isn't available to the account.
- `ttl=` on `Once`, `@once.tool`, `execute` and `aexecute`. Done records expire and the next call runs again; failed and unknown never expire. Stored in a new `expires_at` column, added to existing SQLite and Postgres tables on open. `once gc --expired` and `Once.purge(expired=True)` delete expired records.
- `current_scope()` returns the active `once.scope()` value. An `idempotency_key` parameter that gets `""` or `None` now means "no key", so one is computed. A `""` default used to become the key itself, so two tools with that default hit `KeyConflict`.
- `expires_at` is indexed in both stores, so `gc --expired` doesn't scan the whole table. The index is added to existing tables on open.
- `Once(hooks=Hooks(...))` with `on_claim`, `on_done`, `on_failed`, `on_unknown` and `on_replay`. Hooks get an `Event`, may be async under `aexecute`, and an error in one is logged, not raised.
- OpenTelemetry spans (`once <tool_name>`) behind the `otel` extra. No-op without a configured tracer.

Fixes from a review before the release:

- A sync call with `timeout=` runs in a thread that keeps the caller's context, so `once.scope()` and tracing still apply inside it.
- A `ttl` that would expire after year 9999 is treated as never expiring instead of raising `OverflowError`.
- A claim is given back if the call is interrupted (or an async store step is cancelled) before the tool starts, so the key isn't left pending.
- Storing the outcome is retried once after a store error, so a dropped connection doesn't leave a finished call pending.
- Calling the same key from inside its own call raises `OnceError` instead of deadlocking. This includes a sync call inside an async owner.
- `exclude=` names that are not parameters of the function raise an error.
- `EffectUnknown` is raised even if the record was reset while the call was finishing.
- `stripe_verifier` reads the status of the object it finds: failed or canceled counts as not happened, and a status still in flux raises `StripeInconclusive` so the record stays unknown. It also stays unknown when the list scan runs out before the end.
- `http_verifier` JSON-encodes non-string arguments in the URL, and rejects an argument named `key` that would clash with `{key}`.
- `PostgresStore` takes `statement_timeout` (30 seconds by default) and retries a statement once after a dropped connection. Postgres URLs are accepted in any case and with `+psycopg`.
- The CLI refuses SQLite files without an effects table and rejects a negative `--limit`.
- JSON errors name the argument or the verify function that couldn't be serialized.
- README and docs have a Gotchas section.

Changes made before the release that break keys from earlier development builds:

- `make_key` is now `sha256(canonical_json([tool_name, args, scope]))`. It used to concatenate the parts, which let some tool names and scopes collide.
- `canonical_json` now escapes non-ASCII characters (`ensure_ascii=True`), so strings with lone surrogates can be hashed.

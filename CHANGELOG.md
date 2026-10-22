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

Changes made before the release that break keys from earlier development builds:

- `make_key` is now `sha256(canonical_json([tool_name, args, scope]))`. It used to concatenate the parts, which let some tool names and scopes collide.
- `canonical_json` now escapes non-ASCII characters (`ensure_ascii=True`), so strings with lone surrogates can be hashed.

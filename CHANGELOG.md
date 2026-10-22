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
- `once.integrations.openai_agents`, `once.integrations.pydantic_ai` and `once.integrations.crewai` decorators, with examples and extras.
- `PostgresStore` for workers on several machines, behind the `postgres` extra. Use `Once("postgresql://...")` or `Once(store=PostgresStore(dsn, table=...))`. The CLI accepts a Postgres URL for `--db` and has a `--table` option.
- `Store` protocol, so `Once(store=...)` takes any backend that implements it.
- `once.verifiers`: `stripe_verifier` and `stripe_params` for Stripe charges, PaymentIntents and refunds (`stripe` extra, or pass a client), and `http_verifier` for any API that can look an effect up by key.

Changes made before the release that break keys from earlier development builds:

- `make_key` is now `sha256(canonical_json([tool_name, args, scope]))`. It used to concatenate the parts, which let some tool names and scopes collide.
- `canonical_json` now escapes non-ASCII characters (`ensure_ascii=True`), so strings with lone surrogates can be hashed.

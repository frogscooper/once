# once

`once` makes side-effecting tool calls (charge a card, send an email, create a ticket) safe for an AI agent to retry. Each distinct call runs at most once. A repeat gets the stored result instead of running the side effect again.

Agents retry after timeouts, crash and resume, or re-plan and call the same tool twice. `once` records every call in SQLite or Postgres under an idempotency key, so the duplicates do nothing.

```python
from once import Once

once = Once()  # SQLite in ./once.db


@once.tool
def send_invoice(customer: str, amount: int) -> str:
    return billing.send(customer, amount)


with once.scope("order_456"):
    send_invoice("cus_123", 1000)  # sends
    send_invoice("cus_123", 1000)  # returns the stored result, sends nothing
```

## Install

```
pip install agent-once
```

The import name is `once`. It needs Python 3.10 or newer and has no dependencies. Extras add optional pieces:

| extra | adds |
|-------|------|
| `postgres` | `PostgresStore` (psycopg 3) |
| `stripe` | the Stripe client for `stripe_verifier` |
| `otel` | OpenTelemetry spans |

## Where to go next

- [Quickstart](quickstart.md): a tool, a scope, an explicit key.
- [Concepts](concepts.md): statuses, unknown outcomes, `verify`, `stale_after`, scopes and keys.
- [Expiry and gc](ttl.md): let a result run again after a while.
- [Backends](backends.md): SQLite, Postgres, or your own store.
- [Verifiers](verifiers.md): ready-made `verify` callbacks for Stripe and HTTP.
- [Hooks and OpenTelemetry](hooks.md): see what each call did.
- [Integrations](integrations/index.md): MCP, LangChain, OpenAI Agents SDK, Pydantic AI, CrewAI.
- [CLI](cli.md): inspect and clean up the database.
- [Why not just X](why-not.md): Stripe keys, Temporal, a Redis lock.

`once` is at-most-once, not exactly-once. If a process dies after the effect happened but before the result was written, the call is not run again on its own. [Concepts](concepts.md#stale_after) explains what happens then and how `verify` closes the gap.

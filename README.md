# once

`once` makes side-effecting tool calls (charge a card, send an email, create a ticket) safe for an AI agent to retry. Each distinct call runs at most once; a replay gets the stored result instead of running the side effect again.

Agents retry after timeouts, crash and resume, or re-plan and call the same tool twice. `once` records every call in SQLite (or Postgres) under an idempotency key so the duplicates do nothing.

## Install

```
pip install agent-once
```

The import name is `once`. Python 3.10+, no dependencies. For the Postgres store, `pip install 'agent-once[postgres]'` (adds psycopg 3).

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

The key is `sha256(canonical_json([tool_name, args, scope]))` as hex, where `canonical_json` is `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)`. Putting the three parts in one JSON array means they cannot run into each other, so different inputs always give different keys. A scope of `None` and `""` are different. Arguments are bound to the function signature with defaults filled in, so `charge_card(1000, "cus_123")` and `charge_card(amount=1000, customer="cus_123")` get the same key. Arguments must be JSON serializable.

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

## Verify

An unknown record is stuck until someone checks what happened. If the downstream system can answer that, give the tool a `verify` callback and `once` will ask it on the next call instead of raising `EffectUnknown`. Here the tool stores the key in the charge metadata so it can be found later:

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

`verify(key, args)` gets the key and the call arguments. If it returns a result, the record becomes `done` with that result and nothing runs. If it returns `NOT_HAPPENED`, the record is taken over and the function runs again. If it raises, the record is left as it was and the error propagates. `execute(..., verify=...)` works the same way.

Verify is also used for pending records older than `Once(stale_after=...)` seconds, which covers a process that died mid-call. Pick a value well above the tool's longest run, since a pending record that is merely slow looks the same. A record that was taken over can no longer be written by its old owner. Failed records are never verified, and nothing is re-run without a verify callback saying so.

## Ready-made verifiers

`once.verifiers` has verify callbacks for common cases. They need nothing beyond the standard library unless noted.

**Stripe.** `stripe_params` tags the create params with the once key in metadata and sends the key as Stripe's `Idempotency-Key`. `stripe_verifier` finds the object by that metadata later:

```python
import stripe
from once.verifiers import stripe_params, stripe_verifier

client = stripe.StripeClient("sk_...")


@once.tool(name="charge_card", verify=stripe_verifier("payment_intent", client=client))
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict:
    params = {"amount": amount, "currency": "usd", "customer": customer, "confirm": True}
    return client.v1.payment_intents.create(**stripe_params(idempotency_key, params)).to_dict()
```

The kind is `"payment_intent"`, `"charge"` or `"refund"`. Charges and PaymentIntents are looked up with the search API. Search can take a minute to see new objects, so when it finds nothing the newest `scan` objects (default 100) from the list API are checked too. Refunds have no search, so only the list is used. If search isn't available to the account (Stripe doesn't offer it in India, or a restricted key can't use it), the list is used instead. A `"charge"` lookup only finds charges you tagged yourself; charges made by a PaymentIntent don't copy its metadata, so look up the PaymentIntent instead. `list_params=lambda args: {"customer": args["customer"]}` narrows the list. The verifier returns the object as a plain dict, or `result(obj)` if you pass `result=`, so match it to what the tool returns. Leave `client` out to build a `StripeClient` from `api_key=` or `stripe.api_key`; that needs `pip install 'agent-once[stripe]'`.

Use both helpers together. If a lookup misses, the call runs again with the same `Idempotency-Key`, and Stripe replays the first response instead of charging twice. Stripe keeps idempotency keys for 24 hours.

**HTTP.** `http_verifier` asks your own API. `{key}` in the URL is the once key and `{name}` is the tool argument of that name, both URL-encoded:

```python
from once.verifiers import http_verifier

verify = http_verifier(
    "https://api.example.com/emails/{key}",
    headers={"Authorization": f"Bearer {token}"},
)
```

A 2xx returns the parsed JSON body (or the text, or None when empty). A 404 returns `NOT_HAPPENED`. Anything else raises, so the record stays unknown. Change the mapping with `happened=` and `not_happened=`. `result=` can turn the body into the tool's return value, or into `NOT_HAPPENED` for APIs that answer 200 with something like `{"found": false}`.

## Async

`@once.tool` works on `async def` functions and gives you an async wrapper. For explicit calls use `await once.aexecute(...)`, which takes the same arguments as `execute`.

```python
@once.tool(name="post_message", timeout=10)
async def post_message(channel: str, text: str, idempotency_key: str | None = None) -> dict:
    resp = await http.post(
        "/messages",
        json={"channel": channel, "text": text},
        headers={"Idempotency-Key": idempotency_key},
    )
    return resp.json()


async def run_step(run_id: str) -> None:
    with once.scope(run_id):
        await post_message("#ops", "deploy done")
```

The rules are the same as for sync calls. Coroutines on one event loop with the same key wait for the first one and get its result. A sync and an async caller in the same process, or callers on two event loops, also wait for whoever started first and get its result. Other processes see `pending`, or wait up to `wait` seconds. One exception: a blocking sync call on the event loop thread, for a key that a coroutine on that same loop is running, raises `EffectPending` instead of deadlocking. SQLite calls run in worker threads with `asyncio.to_thread`, so the loop is not blocked.

`timeout=` uses `asyncio.wait_for`; the call is cancelled and the record is marked unknown. If the task is cancelled while the tool runs, the record is marked unknown and `CancelledError` is re-raised. `verify` can be a plain or an async function. `aexecute` also accepts a plain function and runs it in a worker thread.

## Framework helpers

`once.integrations` has decorators for MCP servers, LangChain/LangGraph, the OpenAI Agents SDK, Pydantic AI and CrewAI. They don't import the framework, so there is nothing extra to install. Put them under the framework's own decorator. They leave the framework's context or config parameter out of the key, set the scope from it, and hide `idempotency_key` from the tool schema so the model never sees it.

MCP (`FastMCP` in mcp 1.x, `MCPServer` in 2.x). The scope is `scope_arg` if you name one, otherwise the MCP session id. Request ids are not used, since a retried call gets a new one.

```python
from once.integrations.mcp import once_tool


@server.tool()
@once_tool(once, name="charge_card", scope_arg="order_id")
async def charge_card(order_id: str, amount: int, ctx: Context) -> dict: ...
```

LangChain and LangGraph. The scope is `config["configurable"]["thread_id"]`, so a replayed step in a thread gets the stored result:

```python
from once.integrations.langchain import once_tool


@tool
@once_tool(once)
def send_email(to: str, subject: str, config: RunnableConfig) -> str: ...
```

OpenAI Agents SDK. The scope is `scope_arg`, then `scope_from(ctx)` if given, then `RunConfig.group_id`, so pass your conversation id as `group_id`:

```python
from once.integrations.openai_agents import once_tool


@function_tool
@once_tool(once, name="charge_card")
async def charge_card(ctx: ToolContext[Deps], customer: str, amount: int) -> str: ...
```

Pydantic AI. The scope is `scope_arg`, then `scope_from(ctx)`, then the enclosing `once.scope()`. Pass `scope="conversation"` or `scope="run"` to use `ctx.conversation_id` or `ctx.run_id` instead (Pydantic AI makes up a new conversation id per run unless you pass one):

```python
from once.integrations.pydantic_ai import once_tool


@agent.tool
@once_tool(once, scope="conversation")
def send_email(ctx: RunContext[Deps], to: str) -> str: ...
```

CrewAI. Tools get no run context, so the scope is `scope_arg` or the enclosing `once.scope()`. Wrapping `crew.kickoff()` in `with once.scope(run_id):` works, since CrewAI copies context variables into its worker threads:

```python
from once.integrations.crewai import once_tool


@tool("refund_order")
@once_tool(once, name="refund_order", scope_arg="order_id")
def refund_order(order_id: str, amount: int) -> str:
    """Refund an order."""
```

Runnable versions are in `examples/`, and none of them need an API key. The `openai-agents`, `pydantic-ai` and `crewai` extras install the framework. `crewai` and `openai-agents` can't be installed in the same environment: crewai pins `openai<3` and openai-agents needs `openai>=3`. Every helper also takes `ttl=`, passed through to `@once.tool`. For anything else, `@once.tool(exclude=("ctx",))` leaves a parameter out of the key, and `with once.scope(...)` sets the scope.

## Expiry

By default a done result is kept forever. Give it a time to live when the effect should happen again later, like a daily report or a cache refresh:

```python
from datetime import timedelta

once = Once("once.db", ttl=timedelta(days=30))  # default for every call


@once.tool(ttl=3600)  # this tool: one hour
def send_digest(user_id: str) -> str: ...


once.execute(send_digest, user_id="u_1", ttl=60)  # this call: one minute
```

`ttl` is seconds or a `timedelta`. The call's value beats the tool's, which beats the `Once` default; `math.inf` means never. The clock starts when the call finishes, and the record keeps it in `expires_at`. After that the record counts as gone: the next call with the key runs again (even with different arguments), and if several callers get there at once only one of them runs it. The rest wait for its result like any other pending call.

Only done records expire. Failed and unknown records keep blocking until you `reset` them or `verify` resolves them, since someone has to decide whether the effect happened.

Expired records stay in the table until you delete them with `once gc --expired` or `once.purge(expired=True)`. Run it on a schedule, for example from cron:

```
0 3 * * * once gc --expired --db /srv/app/once.db
```

## CLI

The package installs a `once` command for looking at and cleaning up a database. It reads `--db` or `$ONCE_DB`, defaulting to `./once.db`.

```
once ls [--status unknown] [--limit 20]   # key, status, updated_at, error
once show 3f2a                           # one record as JSON; a unique key prefix works
once reset 3f2a                          # delete it so the call can run again
once gc --older-than 7d                  # delete done records older than 7 days
once gc --older-than 30d --status failed --status done
once gc --expired                        # delete done records past their ttl
```

`gc --older-than` only deletes done records unless you pass `--status`. Deleting a pending or unknown record means the call can run again, so do that with `reset` after checking. The same operations are on `Once` as `records(status=..., limit=...)`, `purge(older_than_seconds, status=..., expired=...)` and `reset(key)`.

## Storage

One table in SQLite, in WAL mode with a busy timeout:

```sql
CREATE TABLE effects (key TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT,
  error TEXT, args_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  expires_at TEXT);
```

Databases and Postgres tables made before `expires_at` existed get the column added when `Once` opens them; old records never expire. With `PostgresStore(create=False)` the user still needs the right to `ALTER` the table once, or you can add the column yourself.

A call claims its key with `INSERT ... ON CONFLICT DO NOTHING`. Whoever inserts the row runs the function. This works across threads and processes on one machine. SQLite over a network filesystem is not supported.

## Postgres

For workers on more than one machine, point them all at the same Postgres database:

```python
from once import Once
from once.postgres import PostgresStore

once = Once("postgresql://user:pass@db/app")  # table once_effects, created if missing

# or pick the table yourself
once = Once(store=PostgresStore("postgresql://user:pass@db/app", table="agent.effects"))
```

Everything works the same as with SQLite: statuses, `verify`, `stale_after`, `aexecute`, `reset`, `records` and `purge`. The claim is the same `INSERT ... ON CONFLICT DO NOTHING`, and finishing or taking over a record is an `UPDATE` that only matches if the row is unchanged, so the database decides who runs the call, not anything in your process. Each `Once` holds one connection in autocommit mode and reopens it if it drops. The CLI takes a URL too: `once ls --db postgresql://... [--table agent.effects]`.

Timestamps come from each worker's clock, so `stale_after` assumes the machines' clocks are roughly in sync. Keep it well above both your longest tool call and any clock drift.

## Limitations

This is at-most-once, not exactly-once. If the process dies after the side effect happens but before the result is written, the record stays `pending` and the call will not run again unless you set `stale_after` and a `verify` callback. That is on purpose: running it twice is the thing we are trying to avoid. You need to look at the downstream system and then `reset` the key or leave it.

The way to close that gap is to forward the key to the downstream API's own idempotency key (Stripe, many payment and messaging APIs support one). Then a retry after `reset` is safe too. A `verify` callback (above) turns the manual check into code.

## License

MIT

# Quickstart

## Decorate a tool

```python
from once import Once

once = Once()  # SQLite in ./once.db, or Once("path/to/once.db")


@once.tool(name="charge_card")
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict:
    charge = stripe.Charge.create(
        amount=amount,
        currency="usd",
        customer=customer,
        idempotency_key=idempotency_key,
    )
    return {"id": charge.id}
```

Register `charge_card` with your agent framework as a normal tool. Because the function has an `idempotency_key` parameter, `once` passes the computed key in, so you can forward it to an API that has its own idempotency keys.

## Give it a scope

The key is built from the tool name, the arguments and a scope. The scope is usually a run, order or step id:

```python
with once.scope("order_456"):
    charge_card(1000, "cus_123")  # charges
    charge_card(1000, "cus_123")  # returns the stored {"id": ...}, no second charge
```

Without a scope, the same call with the same arguments only ever runs once, for good. That is rarely what you want for something like "send a reminder", so set a scope or a [ttl](ttl.md).

## Call it explicitly

`execute` runs a function with your own key. It takes keyword arguments for the function:

```python
from once import make_key

key = make_key("charge_card", {"amount": 1000, "customer": "cus_123"}, scope="order_456")
result = once.execute(charge_card, key=key, amount=1000, customer="cus_123")
```

Leave `key` out and `execute` computes it from `tool_name`, the arguments and `scope=`. When `fn` is a decorated tool, its name, `verify` and `ttl` are used unless you pass others.

## Async

`@once.tool` works on `async def` functions too and gives you an async wrapper. For explicit calls use `await once.aexecute(...)`, which takes the same arguments as `execute` and also accepts a plain function, run in a worker thread.

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

Store calls run in worker threads with `asyncio.to_thread`, so the event loop is not blocked.

## When something goes wrong

The first call re-raises whatever the function raised. Later calls with the same key raise an `Effect*` error that carries the stored `.record`:

```python
from once import EffectFailed, EffectUnknown

try:
    charge_card(1000, "cus_123")
except EffectUnknown as exc:
    print("check Stripe for", exc.record.key)
except EffectFailed as exc:
    print("failed earlier:", exc.record.error)
```

After you have checked what happened, `once.reset(key)` lets the call run again. [Concepts](concepts.md) has the details, and [Verifiers](verifiers.md) shows how to automate the check.

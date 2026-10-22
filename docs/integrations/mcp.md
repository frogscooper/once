# MCP

```
pip install 'agent-once[mcp]'
```

`once.integrations.mcp.once_tool` wraps an MCP server tool. Register the result with your server as usual:

```python
from mcp.server.fastmcp import Context, FastMCP

from once import Once
from once.integrations.mcp import once_tool

mcp = FastMCP("payments")
once = Once("once.db")


@mcp.tool()
@once_tool(once, name="charge_card", scope_arg="order_id")
async def charge_card(
    order_id: str, amount: int, customer: str, ctx: Context, idempotency_key: str | None = None
) -> dict:
    charge = await payments.charge(customer, amount, idempotency_key=idempotency_key)
    return {"charge_id": charge.id}
```

On newer `mcp` releases, import `Context` from `mcp.server.mcpserver` instead.

```python
once_tool(
    once,
    fn=None,
    /,
    *,
    name=None,
    timeout=None,
    ttl=None,
    verify=None,
    scope_arg=None,
    context_arg=None,
)
```

`name`, `timeout`, `ttl` and `verify` work as in `@once.tool`; `ttl` is seconds or a `timedelta` (see [Expiry and gc](../ttl.md)).

The scope is, in order:

1. the value of the `scope_arg` tool argument, if set.
2. the MCP session id, from the `mcp-session-id` header of the request, found through the tool's `Context` argument.
3. the enclosing `once.scope()`, if any.

The context argument is found by its `Context` annotation or the names `ctx` and `context`; `context_arg=` names it explicitly. It is left out of the key. An `idempotency_key` parameter gets the computed key like with `@once.tool`, and is hidden from the tool's signature so the model is never asked for it.

A session id only scopes one client session. Retries from a reconnected client get a new session, so for effects that must happen once per order or per job, use `scope_arg`.

`examples/mcp_server.py` in the repo is a runnable server.

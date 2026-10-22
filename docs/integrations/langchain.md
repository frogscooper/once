# LangChain and LangGraph

```
pip install 'agent-once[langchain]'
```

`once.integrations.langchain.once_tool` wraps a function for a LangChain tool. Put `@tool` on top:

```python
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from once import Once
from once.integrations.langchain import once_tool

once = Once("once.db")


@tool
@once_tool(once, name="send_invoice")
def send_invoice(customer: str, amount: int, config: RunnableConfig) -> str:
    """Send an invoice to a customer."""
    return billing.send(customer, amount)


graph.invoke(inputs, config={"configurable": {"thread_id": "order_456"}})
```

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
    scope_key="thread_id",
    config_arg=None,
)
```

`name`, `timeout`, `ttl` and `verify` work as in `@once.tool`; `ttl` is seconds or a `timedelta` (see [Expiry and gc](../ttl.md)).

The scope is, in order:

1. `config["configurable"][scope_key]`, the LangGraph thread id by default.
2. the enclosing `once.scope()`, if any.

The `config` argument is found by its `RunnableConfig` annotation or the name `config`; `config_arg=` names it explicitly. LangChain fills it in and hides it from the model. It is left out of the key.

When a LangGraph thread resumes from a checkpoint and replays a node, the tool calls it already made return their stored results instead of running again.

Sync and async functions both work. `examples/langgraph_example.py` in the repo is a runnable graph.

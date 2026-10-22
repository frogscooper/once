# Pydantic AI

```
pip install 'agent-once[pydantic-ai]'
```

This installs `pydantic-ai-slim`. `once.integrations.pydantic_ai.once_tool` wraps a tool function. Put `@agent.tool` on top, or pass the function in `Agent(tools=[...])`. Pydantic AI passes `RunContext` as the first parameter and hides it from the model:

```python
from pydantic_ai import Agent, RunContext

from once import Once
from once.integrations.pydantic_ai import once_tool

once = Once("once.db")
agent = Agent("openai:gpt-4o")


@agent.tool
@once_tool(once)
def send_email(ctx: RunContext[None], to: str) -> str:
    """Send an email."""
    return mailer.send(to)


agent.run_sync("email a@x.com", conversation_id="conv_1")
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
    scope_arg=None,
    scope_from=None,
    scope="conversation",
    context_arg=None,
)
```

The scope is, in order:

1. the value of the `scope_arg` tool argument, if set.
2. `scope_from(ctx)`, if given and it returns a value.
3. the enclosing `once.scope()`, if any.
4. `ctx.conversation_id`, or `ctx.run_id` with `scope="run"`. Any other `scope` value raises `ValueError`.

Pass your own `conversation_id` to `agent.run`. Without one, Pydantic AI makes up a fresh id for every run, and each run gets its own key. `run_id` stays the same when a durable workflow replays a run. Tool call ids and retry counts are not used.

The context argument is found by its `RunContext` annotation or the names `ctx` and `context`; `context_arg=` names it explicitly. It is left out of the key.

`examples/pydantic_ai_example.py` runs offline with a `FunctionModel`.

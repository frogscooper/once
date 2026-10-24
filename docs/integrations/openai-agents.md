# OpenAI Agents SDK

```
pip install 'agent-once[openai-agents] @ git+https://github.com/frogscooper/once'
```

This installs `openai-agents>=0.8.4`. Older releases don't give tools `ctx.run_config`, so `RunConfig.group_id` can't be read and calls from different runs with the same arguments would silently share one key.

`once.integrations.openai_agents.once_tool` wraps a function tool. Put `@function_tool` on top. The SDK passes a `ToolContext` (or `RunContextWrapper`) as the first parameter and hides it from the model:

```python
from typing import Any

from agents import Agent, RunConfig, Runner, function_tool
from agents.tool_context import ToolContext

from once import Once
from once.integrations.openai_agents import once_tool

once = Once("once.db")


@function_tool
@once_tool(once, name="charge_card")
async def charge_card(ctx: ToolContext[Any], customer: str, amount: int) -> str:
    """Charge a customer."""
    charge = await payments.charge(customer, amount)
    return charge.id


agent = Agent(name="billing", tools=[charge_card])


async def handle(conversation_id: str, text: str) -> None:
    config = RunConfig(group_id=conversation_id)
    await Runner.run(agent, text, run_config=config)
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
    context_arg=None,
)
```

The scope is, in order:

1. the value of the `scope_arg` tool argument, if set.
2. `scope_from(ctx)`, if given and it returns a value.
3. the run's `RunConfig.group_id`. Set it to your conversation or session id.
4. the enclosing `once.scope()`, if any.

Tool call ids are not used, because a retried call gets a new one.

The context argument is found by its `ToolContext` or `RunContextWrapper` annotation, or the names `ctx`, `context` and `wrapper`; `context_arg=` names it explicitly. It is left out of the key.

!!! note
    The `openai-agents` and `crewai` extras can't be installed together: the Agents SDK needs `openai>=3` and CrewAI pins `openai<3`.

An `idempotency_key` parameter gets the computed key and is hidden from the model, as with the other helpers. `name`, `timeout`, `ttl` and `verify` work as in `@once.tool`.

`examples/openai_agents_example.py` runs offline with a scripted model.

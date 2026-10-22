# CrewAI

```
pip install 'agent-once[crewai]'
```

`once.integrations.crewai.once_tool` wraps a CrewAI tool function. Put `@tool` on top, and give the function a docstring, since CrewAI requires one:

```python
from crewai import Crew
from crewai.tools import tool

from once import Once
from once.integrations.crewai import once_tool

once = Once("once.db")


@tool("refund_order")
@once_tool(once, name="refund_order", scope_arg="order_id")
def refund_order(order_id: str, amount: int) -> str:
    """Refund an order."""
    return payments.refund(order_id, amount)


crew = Crew(agents=[support], tasks=[handle_ticket])

with once.scope("ticket_789"):
    crew.kickoff()
```

```python
once_tool(once, fn=None, /, *, name=None, timeout=None, ttl=None, verify=None, scope_arg=None)
```

CrewAI tools get no run context, so the scope is:

1. the value of the `scope_arg` tool argument, if set.
2. the enclosing `once.scope()`, if any.

Wrap `crew.kickoff()` in `with once.scope(run_id):`. CrewAI copies context variables into its worker threads, so the tools see the scope.

For a `BaseTool` subclass, decorate `_run` (or `_arun`) and pass `name=`. `self` is left out of the key.

!!! note
    The `crewai` and `openai-agents` extras can't be installed together: CrewAI pins `openai<3` and the OpenAI Agents SDK needs `openai>=3`.

`examples/crewai_example.py` runs offline with a fake model.

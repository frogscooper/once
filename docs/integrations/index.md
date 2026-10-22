# Integrations

`@once.tool` works with any framework, since it returns a normal function. The integration helpers add one thing: they take the scope from the framework's own run, thread or session id, so you don't have to wrap each run in `once.scope()`.

| framework | module | extra | scope comes from |
|-----------|--------|-------|------------------|
| [MCP](mcp.md) | `once.integrations.mcp` | `mcp` | the MCP session id |
| [LangChain and LangGraph](langchain.md) | `once.integrations.langchain` | `langchain` | `configurable.thread_id` |
| [OpenAI Agents SDK](openai-agents.md) | `once.integrations.openai_agents` | `openai-agents` | `RunConfig.group_id` |
| [Pydantic AI](pydantic-ai.md) | `once.integrations.pydantic_ai` | `pydantic-ai` | `ctx.conversation_id` |
| [CrewAI](crewai.md) | `once.integrations.crewai` | `crewai` | `once.scope()` around `kickoff()` |

Every helper is called as `once_tool(once, fn, ...)` or used as a decorator, `@once_tool(once, ...)`, and takes `name`, `timeout`, `ttl` and `verify` like `@once.tool`. Most also take `scope_arg`, the name of a tool argument to use as the scope instead, such as an order id. Framework objects like the context or config argument are left out of the key.

The `openai-agents` extra needs `openai-agents>=0.8.4` and `pydantic-ai` needs `pydantic-ai-slim>=1.89`. Older releases don't expose `RunConfig` to tools or `RunContext.conversation_id`, and the helpers would silently fall back to one shared key.

None of these modules import the framework itself, so they import fine without it.

!!! note
    The `crewai` and `openai-agents` extras can't be installed in one environment: CrewAI pins `openai<3` and the OpenAI Agents SDK needs `openai>=3`. Use separate environments if you need both.

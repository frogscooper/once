"""A Pydantic AI agent that sends an email at most once per conversation.

No API key needed: a FunctionModel asks for the same tool call every run, the
way a real model does when a turn is retried.

    pip install agent-once pydantic-ai-slim
    python examples/pydantic_ai_example.py
"""

from __future__ import annotations

import os
import tempfile

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from once import Once
from once.integrations.pydantic_ai import once_tool

once = Once(os.path.join(tempfile.mkdtemp(), "once.db"))
sent: list[str] = []


def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(parts=[ToolCallPart("send_email", {"to": "a@x.com"})])
    return ModelResponse(parts=[TextPart("sent")])


agent: Agent[None, str] = Agent(FunctionModel(model))


@agent.tool
@once_tool(once)
def send_email(ctx: RunContext[None], to: str) -> str:
    """Send an email."""
    sent.append(to)
    return f"msg_{len(sent)}"


def main() -> None:
    for conversation in ["conv_1", "conv_1", "conv_2"]:
        result = agent.run_sync("email a@x.com", conversation_id=conversation)
        print(conversation, result.output)
    print("emails sent:", len(sent))  # 2: one per conversation


if __name__ == "__main__":
    main()

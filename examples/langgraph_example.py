"""A LangGraph graph whose tool node sends an email at most once per thread.

No LLM needed: a fake agent node always asks for the same tool call, the way a
model does when a step is replayed after a crash or a retry.

    pip install git+https://github.com/frogscooper/once langgraph
    python examples/langgraph_example.py
"""

from __future__ import annotations

import os
import tempfile

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from once import Once
from once.integrations.langchain import once_tool

once = Once(os.path.join(tempfile.mkdtemp(), "once.db"))
sent: list[str] = []


@tool
@once_tool(once)
def send_email(to: str, subject: str, config: RunnableConfig) -> str:
    """Send an email."""
    sent.append(to)
    return f"msg_{len(sent)}"


def agent(state: MessagesState) -> dict[str, list[AIMessage]]:
    call = {"name": "send_email", "args": {"to": "a@x.com", "subject": "hi"}, "id": "call_1"}
    return {"messages": [AIMessage(content="", tool_calls=[call])]}


graph = StateGraph(MessagesState)
graph.add_node("agent", agent)
graph.add_node("tools", ToolNode([send_email]))
graph.add_edge(START, "agent")
graph.add_edge("agent", "tools")
app = graph.compile()


def main() -> None:
    for thread_id in ["thread_1", "thread_1", "thread_2"]:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        result = app.invoke({"messages": []}, config)
        print(thread_id, result["messages"][-1].content)
    print("emails sent:", len(sent))  # 2: one per thread


if __name__ == "__main__":
    main()

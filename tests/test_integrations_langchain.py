"""Run the LangChain adapter against langchain-core and langgraph. Skipped if missing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from once import Once
from once.integrations.langchain import once_tool


def test_tool_invoke_and_graph(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    sent: list[str] = []

    @tool
    @once_tool(once)
    def send_email(to: str, subject: str, config: RunnableConfig) -> str:
        """Send an email."""
        sent.append(to)
        return f"msg_{len(sent)}"

    assert set(send_email.args) == {"to", "subject"}
    t1: RunnableConfig = {"configurable": {"thread_id": "t1"}}
    assert send_email.invoke({"to": "a", "subject": "s"}, config=t1) == "msg_1"
    assert send_email.invoke({"to": "a", "subject": "s"}, config=t1) == "msg_1"

    def agent(state: MessagesState) -> dict[str, Any]:
        call = {"name": "send_email", "args": {"to": "b", "subject": "s"}, "id": "c1"}
        return {"messages": [AIMessage(content="", tool_calls=[call])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([send_email]))
    graph.add_edge(START, "agent")
    graph.add_edge("agent", "tools")
    app = graph.compile()
    for thread in ["t2", "t2", "t3"]:
        config: RunnableConfig = {"configurable": {"thread_id": thread}}
        app.invoke({"messages": []}, config)
    assert sent == ["a", "b", "b"]

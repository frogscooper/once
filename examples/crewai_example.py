"""A CrewAI tool that issues a refund at most once per order.

No API key needed: the tool is called directly, twice for the same order, the
way an agent calls it again when a task is retried. In a crew, the same tool
goes in ``Agent(tools=[...])``.

    pip install git+https://github.com/frogscooper/once crewai
    python examples/crewai_example.py
"""

from __future__ import annotations

import os
import tempfile

from crewai.tools import tool

from once import Once
from once.integrations.crewai import once_tool

once = Once(os.path.join(tempfile.mkdtemp(), "once.db"))
refunds: list[str] = []


@tool("refund_order")
@once_tool(once, name="refund_order", scope_arg="order_id")
def refund_order(order_id: str, amount: int) -> str:
    """Refund an order."""
    refunds.append(order_id)
    return f"re_{len(refunds)}"


def main() -> None:
    for order_id in ["order_1", "order_1", "order_2"]:
        print(order_id, refund_order.run(order_id=order_id, amount=500))
    print("refunds made:", len(refunds))  # 2: one per order


if __name__ == "__main__":
    main()

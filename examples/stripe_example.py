"""Charge a card at most once per order, even if the agent retries the tool call.

FakeStripe stands in for the real `stripe` module. Its first request times out
after the charge was made, so the record becomes unknown. On the retry, verify
finds the charge by the key stored in its metadata, and nothing is charged twice.
"""

from __future__ import annotations

from typing import Any, ClassVar

from once import NOT_HAPPENED, Once, make_key


class FakeStripe:
    charges: ClassVar[list[dict[str, Any]]] = []
    flaky: ClassVar[bool] = True

    @classmethod
    def create_charge(cls, amount: int, customer: str, idempotency_key: str) -> str:
        charge_id = f"ch_{len(cls.charges) + 1}"
        cls.charges.append(
            {"id": charge_id, "amount": amount, "metadata": {"once_key": idempotency_key}}
        )
        if cls.flaky:
            cls.flaky = False
            raise TimeoutError("read timed out after the charge went through")
        return charge_id

    @classmethod
    def search(cls, once_key: str) -> list[dict[str, Any]]:
        return [c for c in cls.charges if c["metadata"]["once_key"] == once_key]


def find_charge(key: str, args: dict[str, Any]) -> Any:
    found = FakeStripe.search(once_key=key)
    if not found:
        return NOT_HAPPENED
    return {"charge_id": found[0]["id"]}


once = Once("example.db")


@once.tool(name="charge_card", verify=find_charge)
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict[str, str]:
    """Charge a customer. Agent-facing tool."""
    assert idempotency_key is not None
    charge_id = FakeStripe.create_charge(amount, customer, idempotency_key=idempotency_key)
    return {"charge_id": charge_id}


def main() -> None:
    with once.scope("order_456"):
        try:
            charge_card(1000, "cus_123")
        except TimeoutError as exc:
            print("first try:", exc)
        print("retry:", charge_card(1000, "cus_123"))  # verify finds ch_1
        print("replay:", charge_card(1000, "cus_123"))  # stored result

    key = make_key("charge_card", {"amount": 1000, "customer": "cus_123"}, scope="order_456")
    print("explicit:", once.execute(fn=charge_card, key=key, amount=1000, customer="cus_123"))
    print("charges made:", len(FakeStripe.charges))


if __name__ == "__main__":
    main()

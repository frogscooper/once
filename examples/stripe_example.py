"""Charge a card at most once per order, even if the agent retries the tool call.

Swap FakeStripe for the real `stripe` module to use it for real.
"""

from __future__ import annotations

from typing import Any, ClassVar

from once import EffectUnknown, Once, make_key


class FakeStripe:
    charges: ClassVar[list[dict[str, Any]]] = []

    @classmethod
    def create_charge(cls, amount: int, customer: str, idempotency_key: str) -> str:
        for charge in cls.charges:
            if charge["idempotency_key"] == idempotency_key:
                return str(charge["id"])
        charge_id = f"ch_{len(cls.charges) + 1}"
        cls.charges.append({"id": charge_id, "idempotency_key": idempotency_key})
        return charge_id


once = Once("example.db")


@once.tool(name="charge_card")
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict[str, str]:
    """Charge a customer. Agent-facing tool."""
    assert idempotency_key is not None
    charge_id = FakeStripe.create_charge(amount, customer, idempotency_key=idempotency_key)
    return {"charge_id": charge_id}


def main() -> None:
    with once.scope("order_456"):
        print(charge_card(1000, "cus_123"))
        print(charge_card(1000, "cus_123"))  # replay, no new charge

    key = make_key("charge_card", {"amount": 1000, "customer": "cus_123"}, scope="order_456")
    try:
        print(once.execute(fn=charge_card, key=key, amount=1000, customer="cus_123"))
    except EffectUnknown as exc:
        print("check Stripe before retrying:", exc)
    print("charges made:", len(FakeStripe.charges))


if __name__ == "__main__":
    main()

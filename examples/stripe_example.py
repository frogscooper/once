"""Charge a card at most once per order, even if the agent retries the tool call.

FakeStripe stands in for stripe.StripeClient so this runs offline. Its first
create times out after the PaymentIntent was made, so the record becomes
unknown. On the retry, stripe_verifier finds the PaymentIntent by the once key
in its metadata, and nothing is charged twice.

With real Stripe, use client = stripe.StripeClient("sk_...") and nothing else
changes.
"""

from __future__ import annotations

from typing import Any

from once import Once
from once.verifiers import stripe_params, stripe_verifier


class FakePage:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class FakePaymentIntents:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.flaky = True

    def create(self, params: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        intent = {"id": f"pi_{len(self.created) + 1}", "status": "succeeded", **params}
        self.created.append(intent)
        if self.flaky:
            self.flaky = False
            raise TimeoutError("read timed out after the charge went through")
        return intent

    def search(self, params: dict[str, Any]) -> FakePage:
        return FakePage([])  # new objects take a while to show up in search

    def list(self, params: dict[str, Any]) -> FakePage:
        return FakePage(list(reversed(self.created))[: params["limit"]])


class FakeStripe:
    def __init__(self) -> None:
        self.payment_intents = FakePaymentIntents()


client = FakeStripe()
once = Once("example.db")


@once.tool(name="charge_card", verify=stripe_verifier("payment_intent", client=client))
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Charge a customer. Agent-facing tool."""
    assert idempotency_key is not None
    params = {"amount": amount, "currency": "usd", "customer": customer, "confirm": True}
    return client.payment_intents.create(**stripe_params(idempotency_key, params))


def main() -> None:
    with once.scope("order_456"):
        try:
            charge_card(1000, "cus_123")
        except TimeoutError as exc:
            print("first try:", exc)
        print("retry:", charge_card(1000, "cus_123")["id"])  # verify finds pi_1
        print("replay:", charge_card(1000, "cus_123")["id"])  # stored result
    print("charges made:", len(client.payment_intents.created))


if __name__ == "__main__":
    main()

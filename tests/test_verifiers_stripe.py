from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from once import NOT_HAPPENED, Once, Status
from once.verifiers import StripeInconclusive, stripe_params, stripe_verifier

# An in-memory stand-in for stripe.StripeClient.


class Page:
    def __init__(self, data: list[Any], everything: list[Any] | None = None) -> None:
        self.data = data
        self._everything = data if everything is None else everything

    def auto_paging_iter(self) -> Iterator[Any]:
        return iter(self._everything)


class Service:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.objects: list[dict[str, Any]] = []
        self.indexed: set[str] = set()  # ids search can see, to fake search lag
        self.by_idempotency_key: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next_create = False
        self.search_error: Exception | None = None

    def create(
        self, params: dict[str, Any], options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("create", params))
        idem = (options or {}).get("idempotency_key")
        if idem in self.by_idempotency_key:
            return self.by_idempotency_key[idem]
        obj = {"id": f"{self.prefix}_{len(self.objects) + 1}", **params}
        self.objects.append(obj)
        if idem is not None:
            self.by_idempotency_key[idem] = obj
        if self.fail_next_create:
            self.fail_next_create = False
            raise TimeoutError("read timed out after the request went through")
        return obj

    def search(self, params: dict[str, Any]) -> Page:
        self.calls.append(("search", params))
        if self.search_error is not None:
            raise self.search_error
        visible = [o for o in self.objects if o["id"] in self.indexed]
        return Page(
            [o for o in visible if f":'{o['metadata'].get('once_key')}'" in params["query"]]
        )

    def list(self, params: dict[str, Any] | None = None) -> Page:
        self.calls.append(("list", dict(params or {})))
        params = dict(params or {})
        limit = params.pop("limit", 10)
        newest = [
            o for o in reversed(self.objects) if all(o.get(k) == v for k, v in params.items())
        ]
        return Page(newest[:limit], newest)

    def index_all(self) -> None:
        self.indexed = {o["id"] for o in self.objects}


class V1:
    def __init__(self) -> None:
        self.charges = Service("ch")
        self.payment_intents = Service("pi")
        self.refunds = Service("re")


class FakeStripeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self.v1 = V1()


@pytest.fixture
def client() -> FakeStripeClient:
    return FakeStripeClient()


def charge_tool(client: FakeStripeClient) -> Any:
    def charge(amount: int, customer: str, idempotency_key: str | None = None) -> Any:
        assert idempotency_key is not None
        return client.v1.payment_intents.create(
            **stripe_params(idempotency_key, {"amount": amount, "customer": customer})
        )

    return charge


def test_stripe_params_tags_metadata_and_idempotency_key() -> None:
    params = {"amount": 5, "metadata": {"order": "o1"}}
    out = stripe_params("k1", params)
    assert out == {
        "params": {"amount": 5, "metadata": {"order": "o1", "once_key": "k1"}},
        "options": {"idempotency_key": "k1"},
    }
    assert params == {"amount": 5, "metadata": {"order": "o1"}}
    assert stripe_params("k", {}, metadata_key="mine")["params"] == {"metadata": {"mine": "k"}}


def test_unknown_charge_is_found_by_list_while_search_lags(
    db: str | Path, client: FakeStripeClient
) -> None:
    once = Once(db)
    pis = client.v1.payment_intents
    verify = stripe_verifier(
        "payment_intent", client=client, list_params=lambda a: {"customer": a["customer"]}
    )
    tool = charge_tool(client)
    pis.fail_next_create = True
    with pytest.raises(TimeoutError):
        once.execute(
            tool, key="k1", tool_name="charge", verify=verify, amount=100, customer="cus_1"
        )
    record = once.get("k1")
    assert record is not None and record.status is Status.UNKNOWN

    result = once.execute(
        tool, key="k1", tool_name="charge", verify=verify, amount=100, customer="cus_1"
    )
    assert result["id"] == "pi_1"
    assert result["metadata"] == {"once_key": "k1"}
    assert len(pis.objects) == 1
    assert ("list", {"customer": "cus_1", "limit": 100}) in pis.calls
    record = once.get("k1")
    assert record is not None and record.status is Status.DONE
    once.close()


def test_search_hit_skips_the_list(client: FakeStripeClient) -> None:
    pis = client.v1.payment_intents
    pis.create(**stripe_params("other", {"amount": 1}))
    pis.create(**stripe_params("k1", {"amount": 2}))
    pis.index_all()
    found = stripe_verifier("payment_intent", client=client)("k1", {})
    assert found["id"] == "pi_2"
    assert [c[0] for c in pis.calls if c[0] != "create"] == ["search"]
    assert pis.calls[-1][1] == {"query": "metadata['once_key']:'k1'", "limit": 10}


def test_not_happened_reruns_once(db: str | Path, client: FakeStripeClient) -> None:
    once = Once(db)
    pis = client.v1.payment_intents
    tool = charge_tool(client)
    verify = stripe_verifier("payment_intent", client=client)
    # The request failed before Stripe saw it: nothing to find, so it runs again.
    with pytest.raises(TimeoutError):
        once.execute(_raise_timeout, key="k1", tool_name="charge", amount=1, customer="c")
    assert verify("k1", {}) is NOT_HAPPENED
    result = once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    assert result["id"] == "pi_1" and len(pis.objects) == 1
    once.close()


def _raise_timeout(amount: int, customer: str) -> None:
    raise TimeoutError("connect timed out")


def test_idempotency_key_covers_a_missed_lookup(client: FakeStripeClient, tmp_path: Path) -> None:
    # scan=0 and a lagging search miss the object, the rerun hits Stripe's replay
    once = Once(tmp_path / "once.db")
    pis = client.v1.payment_intents
    tool = charge_tool(client)
    verify = stripe_verifier("payment_intent", client=client, scan=0)
    pis.fail_next_create = True
    with pytest.raises(TimeoutError):
        once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    result = once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    assert result["id"] == "pi_1"
    assert len(pis.objects) == 1
    once.close()


def test_refund_uses_list_with_filters(client: FakeStripeClient) -> None:
    refunds = client.v1.refunds
    refunds.create(**stripe_params("k1", {"payment_intent": "pi_9"}))
    refunds.create(**stripe_params("k2", {"payment_intent": "pi_8"}))
    verify = stripe_verifier(
        "refund", client=client, list_params=lambda a: {"payment_intent": a["pi"]}, scan=5
    )
    assert verify("k1", {"pi": "pi_9"})["id"] == "re_1"
    assert verify("k2", {"pi": "pi_9"}) is NOT_HAPPENED
    assert all(c[0] != "search" for c in refunds.calls)
    assert refunds.calls[-1] == ("list", {"payment_intent": "pi_9", "limit": 5})


def test_scan_stops_at_the_limit(client: FakeStripeClient) -> None:
    refunds = client.v1.refunds
    refunds.create(**stripe_params("old", {}))
    for i in range(150):
        refunds.create(**stripe_params(f"k{i}", {}))
    with pytest.raises(StripeInconclusive, match="newest 150"):
        stripe_verifier("refund", client=client, scan=150)("old", {})
    assert stripe_verifier("refund", client=client, scan=151)("old", {})["id"] == "re_1"
    assert refunds.calls[-1][1]["limit"] == 100
    assert stripe_verifier("refund", client=client, scan=151)("gone", {}) is NOT_HAPPENED


def test_a_miss_past_the_scan_window_stays_unknown(
    client: FakeStripeClient, tmp_path: Path
) -> None:
    once = Once(tmp_path / "once.db")
    pis = client.v1.payment_intents
    tool = charge_tool(client)
    verify = stripe_verifier("payment_intent", client=client, scan=5)
    pis.fail_next_create = True
    with pytest.raises(TimeoutError):
        once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    for i in range(5):
        pis.create(**stripe_params(f"later{i}", {"amount": 1}))
    with pytest.raises(StripeInconclusive):
        once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    record = once.get("k1")
    assert record is not None and record.status is Status.UNKNOWN
    assert len(pis.objects) == 6
    once.close()


def test_has_more_on_a_plain_page_means_incomplete() -> None:
    class Charges:
        def __init__(self, has_more: bool) -> None:
            self.has_more = has_more

        def search(self, params: dict[str, Any]) -> Page:
            return Page([])

        def list(self, params: dict[str, Any]) -> Any:
            return types.SimpleNamespace(data=[], has_more=self.has_more)

    with pytest.raises(StripeInconclusive):
        stripe_verifier("charge", client=types.SimpleNamespace(charges=Charges(True)))("k", {})
    verify = stripe_verifier("charge", client=types.SimpleNamespace(charges=Charges(False)))
    assert verify("k", {}) is NOT_HAPPENED


def test_clients_without_v1_and_object_shaped_results() -> None:
    class Obj:
        def __init__(self, id: str, key: str) -> None:
            self.id = id
            self.metadata = types.SimpleNamespace(once_key=key)

        def to_dict(self) -> dict[str, Any]:
            return {"id": self.id, "amount": 3}

    class Charges:
        def search(self, params: dict[str, Any]) -> Page:
            return Page([])

        def list(self, params: dict[str, Any]) -> Any:
            return types.SimpleNamespace(data=[Obj("ch_a", "x"), Obj("ch_b", "k1")])

    old_style = types.SimpleNamespace(charges=Charges())
    verify = stripe_verifier("charge", client=old_style, result=lambda c: c["id"])
    assert verify("k1", {}) == "ch_b"
    assert verify("nope", {}) is NOT_HAPPENED


def test_search_query_escapes_quotes(client: FakeStripeClient) -> None:
    charges = client.v1.charges
    stripe_verifier("charge", client=client, metadata_key="a'b", scan=0)("it's\\", {})
    assert charges.calls[-1][1]["query"] == "metadata['a\\'b']:'it\\'s\\\\'"


def test_result_is_json_safe(client: FakeStripeClient) -> None:
    import datetime

    charges = client.v1.charges
    charges.objects.append(
        {"id": "ch_1", "metadata": {"once_key": "k"}, "at": datetime.date(2026, 1, 2)}
    )
    assert stripe_verifier("charge", client=client)("k", {})["at"] == "2026-01-02"


def test_stripe_errors_leave_the_record_unknown(client: FakeStripeClient, tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    client.v1.charges.search_error = ConnectionError("stripe is down")
    verify = stripe_verifier("charge", client=client)
    with pytest.raises(TimeoutError):
        once.execute(_raise_timeout, key="k", tool_name="t", amount=1, customer="c")
    with pytest.raises(ConnectionError):
        once.execute(_raise_timeout, key="k", tool_name="t", verify=verify, amount=1, customer="c")
    record = once.get("k")
    assert record is not None and record.status is Status.UNKNOWN
    once.close()


def test_bad_arguments() -> None:
    with pytest.raises(ValueError, match="unsupported kind"):
        stripe_verifier("customer", client=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scan"):
        stripe_verifier("charge", client=object(), scan=-1)


def test_default_client_needs_stripe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "stripe", None)
    verify = stripe_verifier("charge", api_key="sk_test")
    with pytest.raises(ImportError, match=r"agent-once\[stripe\]"):
        verify("k", {})


def test_default_client_is_made_once(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[FakeStripeClient] = []

    def make(api_key: str) -> FakeStripeClient:
        made.append(FakeStripeClient(api_key))
        return made[-1]

    module = types.ModuleType("stripe")
    module.StripeClient = make  # type: ignore[attr-defined]
    module.api_key = "sk_global"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", module)
    verify = stripe_verifier("payment_intent")
    assert verify("k", {}) is NOT_HAPPENED
    assert verify("k", {}) is NOT_HAPPENED
    assert [c.api_key for c in made] == ["sk_global"]
    assert stripe_verifier("charge", api_key="sk_mine")("k", {}) is NOT_HAPPENED
    assert made[-1].api_key == "sk_mine"


class FakeStripeError(Exception):
    def __init__(self, message: str, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


def test_search_unavailable_falls_back_to_the_list(
    db: str | Path, client: FakeStripeClient
) -> None:
    once = Once(db)
    pis = client.v1.payment_intents
    pis.search_error = FakeStripeError(
        "Request req_1: The search feature is temporarily unavailable in your region.", 400
    )
    verify = stripe_verifier("payment_intent", client=client)
    tool = charge_tool(client)
    pis.fail_next_create = True
    with pytest.raises(TimeoutError):
        once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    result = once.execute(tool, key="k1", tool_name="charge", verify=verify, amount=1, customer="c")
    assert result["id"] == "pi_1" and len(pis.objects) == 1
    assert verify("k1", {})["id"] == "pi_1"
    assert [c[0] for c in pis.calls].count("search") == 1
    once.close()


@pytest.mark.parametrize(
    ("message", "status", "falls_back"),
    [
        ("Search is not available to merchants in India", 400, True),
        ("search feature unavailable for merchant", 404, True),
        ("The provided key does not have the required permissions", 403, True),
        ("Invalid query: unknown field", 400, False),
        ("Search is unavailable right now", 503, False),
        ("Too many requests", 429, False),
    ],
)
def test_only_unavailable_search_falls_back(
    client: FakeStripeClient, message: str, status: int, falls_back: bool
) -> None:
    client.v1.charges.objects.append({"id": "ch_1", "metadata": {"once_key": "k"}})
    client.v1.charges.search_error = FakeStripeError(message, status)
    verify = stripe_verifier("charge", client=client)
    if falls_back:
        assert verify("k", {})["id"] == "ch_1"
    else:
        with pytest.raises(FakeStripeError):
            verify("k", {})


def test_unavailable_search_raises_without_a_scan(client: FakeStripeClient) -> None:
    client.v1.charges.search_error = FakeStripeError("search is not available", 400)
    with pytest.raises(FakeStripeError):
        stripe_verifier("charge", client=client, scan=0)("k", {})
    client.v1.charges.search_error = ConnectionError("down")
    with pytest.raises(ConnectionError):
        stripe_verifier("charge", client=client)("k", {})

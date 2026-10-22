"""stripe_verifier and stripe_params against the real stripe package.

The client talks to a local server that answers like the Stripe API, so this
checks the request shapes without any network access.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from once import Once
from once.verifiers import stripe_params, stripe_verifier

stripe = pytest.importorskip("stripe")


class FakeApi:
    def __init__(self) -> None:
        self.intents: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, list[str]], dict[str, str]]] = []
        self.fail_next_create = False
        self.search_error: tuple[int, dict[str, Any]] | None = None
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, value: Any) -> None:
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                api.requests.append(("GET", url.path, query, dict(self.headers)))
                if url.path == "/v1/payment_intents/search" and api.search_error:
                    self._send(*api.search_error)
                elif url.path == "/v1/payment_intents/search":
                    # search lags: it never sees anything here
                    self._send(200, {"object": "search_result", "data": [], "has_more": False})
                elif url.path == "/v1/payment_intents":
                    data = list(reversed(api.intents))
                    self._send(200, {"object": "list", "data": data, "has_more": False})
                else:
                    self._send(404, {"error": {"message": "no route"}})

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                form = parse_qs(self.rfile.read(length).decode())
                api.requests.append(("POST", self.path, form, dict(self.headers)))
                intent = {
                    "id": f"pi_{len(api.intents) + 1}",
                    "object": "payment_intent",
                    "amount": int(form["amount"][0]),
                    "metadata": {"once_key": form["metadata[once_key]"][0]},
                }
                api.intents.append(intent)
                if api.fail_next_create:
                    api.fail_next_create = False
                    self._send(500, {"error": {"message": "boom", "type": "api_error"}})
                    return
                self._send(200, intent)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def api() -> Iterator[FakeApi]:
    a = FakeApi()
    thread = threading.Thread(target=a.httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield a
    a.httpd.shutdown()
    a.httpd.server_close()


def test_real_client_round_trip(api: FakeApi, tmp_path: Path) -> None:
    client = stripe.StripeClient(
        "sk_test_123", base_addresses={"api": api.url}, max_network_retries=0
    )
    once = Once(tmp_path / "once.db", unknown_on=(stripe.APIError,))
    verify = stripe_verifier("payment_intent", client=client)

    @once.tool(name="charge", verify=verify)
    def charge(amount: int, idempotency_key: str | None = None) -> dict[str, Any]:
        assert idempotency_key is not None
        intent = client.v1.payment_intents.create(
            **stripe_params(idempotency_key, {"amount": amount, "currency": "usd"})
        )
        result: dict[str, Any] = intent.to_dict()
        return result

    api.fail_next_create = True
    with once.scope("order_1"), pytest.raises(stripe.APIError):
        charge(500)
    with once.scope("order_1"):
        result = charge(500)

    assert result["id"] == "pi_1" and result["amount"] == 500
    assert len(api.intents) == 1
    post = next(r for r in api.requests if r[0] == "POST")
    key = post[2]["metadata[once_key]"][0]
    assert post[3]["Idempotency-Key"] == key
    search = next(r for r in api.requests if r[1] == "/v1/payment_intents/search")
    assert search[2]["query"] == [f"metadata['once_key']:'{key}'"]
    assert any(r[1] == "/v1/payment_intents" and r[0] == "GET" for r in api.requests)
    once.close()


def error(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error"}}


REGION = error("The search feature is temporarily unavailable in your region.")


@pytest.mark.parametrize(
    ("status", "body", "falls_back"),
    [
        (400, REGION, True),
        (403, error("key lacks permission"), True),
        (400, error("Invalid query"), False),
        (429, error("Too many requests"), False),
        (401, error("Invalid API Key"), False),
    ],
)
def test_search_unavailable_uses_the_list(
    api: FakeApi, status: int, body: dict[str, Any], falls_back: bool
) -> None:
    client = stripe.StripeClient(
        "sk_test_123", base_addresses={"api": api.url}, max_network_retries=0
    )
    api.intents.append({"id": "pi_1", "object": "payment_intent", "metadata": {"once_key": "k"}})
    api.search_error = (status, body)
    verify = stripe_verifier("payment_intent", client=client)
    if not falls_back:
        with pytest.raises(stripe.StripeError):
            verify("k", {})
        return
    assert verify("k", {})["id"] == "pi_1"
    assert verify("k", {})["id"] == "pi_1"
    searches = [r for r in api.requests if r[1] == "/v1/payment_intents/search"]
    assert len(searches) == 1  # not asked again once it said no

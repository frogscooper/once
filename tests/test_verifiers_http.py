from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from once import NOT_HAPPENED, Once, Status
from once.verifiers import http_verifier

# path -> (status, body, content type)
Routes = dict[str, tuple[int, bytes, str]]


class Server:
    def __init__(self) -> None:
        self.routes: Routes = {}
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                server.seen.append((self.command, self.path, dict(self.headers)))
                status, body, ctype = server.routes.get(self.path, (404, b"", "text/plain"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _reply

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def json(self, path: str, value: Any, status: int = 200) -> None:
        self.routes[path] = (status, json.dumps(value).encode(), "application/json")


@pytest.fixture
def server() -> Iterator[Server]:
    s = Server()
    thread = threading.Thread(target=s.httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def test_2xx_returns_parsed_json(server: Server) -> None:
    server.json("/emails/k1", {"id": "em_1", "sent": True})
    verify = http_verifier(server.url + "/emails/{key}")
    assert verify("k1", {}) == {"id": "em_1", "sent": True}


def test_404_is_not_happened(server: Server) -> None:
    assert http_verifier(server.url + "/emails/{key}")("k1", {}) is NOT_HAPPENED


def test_other_statuses_raise(server: Server) -> None:
    server.routes["/emails/k1"] = (503, b"try later", "text/plain")
    with pytest.raises(RuntimeError, match=r"HTTP 503.*try later"):
        http_verifier(server.url + "/emails/{key}")("k1", {})


def test_empty_and_text_bodies(server: Server) -> None:
    server.routes["/a"] = (204, b"", "text/plain")
    server.routes["/b"] = (200, b"sent", "text/plain")
    assert http_verifier(server.url + "/a")("k", {}) is None
    assert http_verifier(server.url + "/b")("k", {}) == "sent"


def test_template_fills_and_quotes_args(server: Server) -> None:
    verify = http_verifier(server.url + "/orders/{order_id}/emails/{key}?to={to}")
    assert verify("a/b c", {"order_id": 7, "to": "x@y.com"}) is NOT_HAPPENED
    assert server.seen[-1][1] == "/orders/7/emails/a%2Fb%20c?to=x%40y.com"
    with pytest.raises(KeyError):
        verify("k", {})


def test_method_and_headers(server: Server) -> None:
    server.json("/check", {"ok": 1})
    verify = http_verifier(
        server.url + "/check", method="POST", headers={"Authorization": "Bearer t"}
    )
    assert verify("k", {}) == {"ok": 1}
    method, _, headers = server.seen[-1]
    assert method == "POST" and headers["Authorization"] == "Bearer t"


def test_custom_status_mapping(server: Server) -> None:
    server.json("/gone", {"error": "gone"}, status=410)
    server.json("/made", {"id": 1}, status=201)
    server.json("/ok", {"id": 2})
    verify_gone = http_verifier(server.url + "/gone", not_happened={404, 410})
    assert verify_gone("k", {}) is NOT_HAPPENED
    strict = http_verifier(server.url + "/{path}", happened={201})
    assert strict("k", {"path": "made"}) == {"id": 1}
    with pytest.raises(RuntimeError, match="HTTP 200"):
        strict("k", {"path": "ok"})
    with pytest.raises(ValueError, match="both"):
        http_verifier(server.url, happened={200, 404})


def test_result_maps_the_body(server: Server) -> None:
    server.json("/a", {"found": False})
    server.json("/b", {"found": True, "id": "x"})

    def pick(body: Any) -> Any:
        return body["id"] if body["found"] else NOT_HAPPENED

    verify = http_verifier(server.url + "/{path}", result=pick)
    assert verify("k", {"path": "a"}) is NOT_HAPPENED
    assert verify("k", {"path": "b"}) == "x"


def test_network_error_raises() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(OSError):
        http_verifier(f"http://127.0.0.1:{port}/x", timeout=2)("k", {})


def test_resolves_an_unknown_record(db: str | Path, server: Server) -> None:
    once = Once(db)
    sent: list[str] = []

    def send_email(to: str, idempotency_key: str | None = None) -> dict[str, str]:
        assert idempotency_key is not None
        sent.append(to)
        server.json(f"/emails/{idempotency_key}", {"id": "em_1", "to": to})
        raise TimeoutError("read timed out")

    verify = http_verifier(server.url + "/emails/{key}")
    with pytest.raises(TimeoutError):
        once.execute(send_email, key="k1", tool_name="email", verify=verify, to="a@b.c")
    result = once.execute(send_email, key="k1", tool_name="email", verify=verify, to="a@b.c")
    assert result == {"id": "em_1", "to": "a@b.c"}
    assert sent == ["a@b.c"]
    record = once.get("k1")
    assert record is not None and record.status is Status.DONE

    # 404 means it never happened, so the second key runs again
    calls: list[str] = []

    def flaky(to: str) -> str:
        calls.append(to)
        if len(calls) == 1:
            raise TimeoutError("connect timed out")
        return "sent"

    with pytest.raises(TimeoutError):
        once.execute(flaky, key="k2", tool_name="email", verify=verify, to="a@b.c")
    assert once.execute(flaky, key="k2", tool_name="email", verify=verify, to="a@b.c") == "sent"
    assert len(calls) == 2
    once.close()


def test_works_from_aexecute(db: str | Path, server: Server) -> None:
    once = Once(db)
    server.json("/emails/k1", {"id": "em_1"})

    async def send(to: str) -> str:
        raise TimeoutError("read timed out")

    async def run() -> Any:
        verify = http_verifier(server.url + "/emails/{key}")
        with pytest.raises(TimeoutError):
            await once.aexecute(send, key="k1", tool_name="email", verify=verify, to="x")
        return await once.aexecute(send, key="k1", tool_name="email", verify=verify, to="x")

    assert asyncio.run(run()) == {"id": "em_1"}
    once.close()

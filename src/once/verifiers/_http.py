"""Verify against any HTTP endpoint that can look an effect up by key."""

from __future__ import annotations

import json
import string
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping
from typing import Any
from urllib.parse import quote

from ..core import NOT_HAPPENED, Verifier

OK = range(200, 300)


def http_verifier(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    happened: Collection[int] = OK,
    not_happened: Collection[int] = (404,),
    timeout: float = 10.0,
    result: Callable[[Any], Any] | None = None,
) -> Verifier:
    """Build a verifier that asks an HTTP endpoint whether the effect happened.

    url is a template. ``{key}`` is the once key and any other ``{name}`` is
    the tool argument of that name, each percent-encoded, for example
    ``"https://api.example.com/orders/{order_id}/emails/{key}"``. Strings go
    in as they are; other values as JSON, so None is ``null`` and True is
    ``true``. A tool argument named ``key`` with ``{key}`` in the template
    raises ValueError, since the template can't tell the two apart.

    A status in ``happened`` returns the response body: parsed JSON, the text
    if it is not JSON, or None if it is empty. A status in ``not_happened``
    returns NOT_HAPPENED, so the call runs again. Any other status, or a
    network error, raises, which leaves the record unknown.

    ``result`` maps the body to what the tool itself returns, so a verified
    result looks like a normal one. It may also return NOT_HAPPENED, for APIs
    that answer 200 with something like ``{"found": false}``.
    """
    happened = frozenset(happened)
    not_happened = frozenset(not_happened)
    if happened & not_happened:
        raise ValueError("a status can't be in both happened and not_happened")
    uses_key = "key" in {field for _, field, _, _ in string.Formatter().parse(url) if field}

    def verify(key: str, args: dict[str, Any]) -> Any:
        if uses_key and "key" in args:
            raise ValueError(
                "the tool has an argument named 'key', so {key} in the url is ambiguous; "
                "rename the argument"
            )
        fields = {name: quote(_text(value), safe="") for name, value in args.items()}
        fields["key"] = quote(key, safe="")
        request = urllib.request.Request(
            url.format_map(fields), method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        if status in not_happened:
            return NOT_HAPPENED
        if status not in happened:
            snippet = body[:200].decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {status} from {request.full_url}: {snippet}")
        value = _parse(body)
        return result(value) if result is not None else value

    return verify


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _parse(body: bytes) -> Any:
    if not body.strip():
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text

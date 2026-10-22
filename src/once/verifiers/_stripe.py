"""Find Stripe objects created by a once call, by the key in their metadata."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from ..core import NOT_HAPPENED, Verifier

Kind = Literal["charge", "payment_intent", "refund"]

METADATA_KEY = "once_key"

# Stripe search covers these. Refunds have no search endpoint.
_SEARCHABLE = {"charge", "payment_intent"}

# How Stripe says search can't be used here, for example for accounts in India
# ("The search feature is temporarily unavailable in your region.").
_UNAVAILABLE = re.compile(
    r"search.{0,40}(unavailable|not available|not enabled|not supported)"
    r"|(unavailable|not available|not enabled|not supported).{0,40}search",
    re.IGNORECASE | re.DOTALL,
)


def stripe_params(
    key: str, params: Mapping[str, Any], *, metadata_key: str = METADATA_KEY
) -> dict[str, Any]:
    """Tag create params with the once key and use it as the Idempotency-Key.

    Returns keyword arguments for a StripeClient create call::

        client.v1.payment_intents.create(**stripe_params(idempotency_key, {...}))

    Existing metadata is kept. The Idempotency-Key makes Stripe replay the
    first response if the same request is sent again within 24 hours.
    """
    tagged = dict(params)
    tagged["metadata"] = {**dict(params.get("metadata") or {}), metadata_key: key}
    return {"params": tagged, "options": {"idempotency_key": key}}


def stripe_verifier(
    kind: Kind,
    *,
    client: Any = None,
    api_key: str | None = None,
    metadata_key: str = METADATA_KEY,
    list_params: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    scan: int = 100,
    result: Callable[[dict[str, Any]], Any] | None = None,
) -> Verifier:
    """Build a verifier that finds the Stripe object tagged with the once key.

    kind is "charge", "payment_intent", or "refund". client is a
    stripe.StripeClient, or anything shaped like one (tests pass a fake). If
    it is None, a StripeClient is made on first use from api_key (or
    stripe.api_key), which needs the stripe package
    (``pip install agent-once[stripe]``).

    Charges and PaymentIntents are looked up with the search API first. Search
    can lag new objects by a minute or so, so if it finds nothing, the newest
    ``scan`` objects from the list API are checked too. Refunds have no search,
    so only the list is used. ``list_params`` maps the tool args to list filters
    such as ``{"customer": args["customer"]}``, to narrow the scan.

    If search isn't available to the account (Stripe doesn't offer it in some
    regions, or a restricted key can't use it), the list is used instead and
    search is skipped from then on. With ``scan=0`` that error is raised.

    Returns the object as a plain dict (or ``result(obj)`` if given), or
    NOT_HAPPENED. Other Stripe errors propagate, which leaves the record unknown.
    """
    if kind not in ("charge", "payment_intent", "refund"):
        raise ValueError(f"unsupported kind {kind!r}")
    if scan < 0:
        raise ValueError("scan must be >= 0")

    made: list[Any] = []
    searchable = [kind in _SEARCHABLE]

    def verify(key: str, args: dict[str, Any]) -> Any:
        if client is not None:
            service = _service(client, kind)
        else:
            if not made:
                made.append(_default_client(api_key))
            service = _service(made[0], kind)
        found = None
        if searchable[0]:
            try:
                found = _first(_search(service, metadata_key, key), metadata_key, key)
            except Exception as exc:
                if not scan or not _search_unavailable(exc):
                    raise
                searchable[0] = False
        if found is None and scan:
            params = {**dict(list_params(args) if list_params else {}), "limit": min(scan, 100)}
            found = _first(_take(service.list(params=params), scan), metadata_key, key)
        if found is None:
            return NOT_HAPPENED
        obj = _to_dict(found)
        return result(obj) if result is not None else obj

    return verify


def _search_unavailable(exc: BaseException) -> bool:
    """True if a Stripe error means search can't be used by this account.

    That is a permission error (HTTP 403), or a 400 or 404 whose message says
    search is unavailable. Rate limits, outages, bad keys and bad queries are
    not, and still raise.
    """
    status = getattr(exc, "http_status", None)
    if status == 403:
        return True
    return status in (400, 404) and _UNAVAILABLE.search(str(exc)) is not None


def _default_client(api_key: str | None) -> Any:
    try:
        stripe = importlib.import_module("stripe")
    except ImportError as exc:
        raise ImportError(
            "stripe_verifier needs the stripe package or a client: pip install 'agent-once[stripe]'"
        ) from exc
    return stripe.StripeClient(api_key or stripe.api_key)


def _service(client: Any, kind: str) -> Any:
    # stripe >= 12 groups services under client.v1, older clients have them on top
    return getattr(getattr(client, "v1", client), kind + "s")


def _search(service: Any, metadata_key: str, key: str) -> Iterable[Any]:
    query = f"metadata[{_quote(metadata_key)}]:{_quote(key)}"
    page = service.search(params={"query": query, "limit": 10})
    return list(page.data)


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _take(page: Any, limit: int) -> Iterable[Any]:
    items = page.auto_paging_iter() if hasattr(page, "auto_paging_iter") else page.data
    for i, item in enumerate(items):
        if i >= limit:
            return
        yield item


def _first(items: Iterable[Any], metadata_key: str, key: str) -> Any:
    for item in items:
        metadata = _get(item, "metadata") or {}
        if _get(metadata, metadata_key) == key:
            return item
    return None


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    try:
        return obj[name]
    except (KeyError, TypeError):
        return getattr(obj, name, None)


def _to_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    data: dict[str, Any] = json.loads(json.dumps(obj, default=str))
    return data

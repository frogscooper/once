# Verifiers

`once.verifiers` has ready-made [verify](concepts.md#verify) callbacks:

```python
from once.verifiers import http_verifier, stripe_params, stripe_verifier
```

Nothing there imports a third party package until it is used.

## Stripe

`stripe_params` tags the create params with the once key in metadata and sends the key as Stripe's `Idempotency-Key`. `stripe_verifier` finds the object by that metadata later:

```python
import stripe

from once.verifiers import stripe_params, stripe_verifier

client = stripe.StripeClient("sk_...")


@once.tool(name="charge_card", verify=stripe_verifier("payment_intent", client=client))
def charge_card(amount: int, customer: str, idempotency_key: str | None = None) -> dict:
    params = {"amount": amount, "currency": "usd", "customer": customer, "confirm": True}
    return client.v1.payment_intents.create(**stripe_params(idempotency_key, params)).to_dict()
```

### stripe_params

```python
stripe_params(key, params, *, metadata_key="once_key")
```

Returns `{"params": ..., "options": {"idempotency_key": key}}`, the keyword arguments a `StripeClient` create call takes. Existing metadata is kept and `metadata[metadata_key]` is set to the key.

### stripe_verifier

```python
stripe_verifier(
    kind,
    *,
    client=None,
    api_key=None,
    metadata_key="once_key",
    list_params=None,
    scan=100,
    result=None,
)
```

- `kind` is `"payment_intent"`, `"charge"` or `"refund"`.
- Charges and PaymentIntents are looked up with the search API first. Search can lag new objects by a minute or so, so if it finds nothing, the newest `scan` objects from the list API are checked too. Refunds have no search, so only the list is used.
- If search isn't available to the account (Stripe doesn't offer it in some regions, or a restricted key can't use it), the list is used instead and search is skipped from then on. With `scan=0` that error is raised.
- `list_params` maps the tool arguments to list filters, like `lambda args: {"customer": args["customer"]}`, to narrow the scan.
- It returns the object as a plain dict, or `result(obj)` if you pass `result=`, so match it to what the tool returns. If nothing matches it returns `NOT_HAPPENED`. Other Stripe errors propagate, which leaves the record unknown.
- The status of the object it finds decides the outcome:

    | kind | returned (happened) | `NOT_HAPPENED` |
    | --- | --- | --- |
    | `charge` | `succeeded` | `failed` |
    | `payment_intent` | `succeeded`, `requires_capture` | `canceled` |
    | `refund` | `succeeded`, `pending` | `failed`, `canceled` |

    Any other status, such as `processing`, `requires_action` or `requires_payment_method`, raises `StripeInconclusive`, so the record stays unknown until it settles and you retry or `reset` it.
- It only returns `NOT_HAPPENED` after reading the list to the end. If more than `scan` objects match the filters and the one it wants isn't among the newest `scan`, it raises `StripeInconclusive` and the record stays unknown. Narrow `list_params` or raise `scan`. With `scan=0` a search miss alone returns `NOT_HAPPENED`, so only use it with `stripe_params`.
- `client` is a `stripe.StripeClient`, or anything shaped like one. Leave it out to build a `StripeClient` on first use from `api_key=` or `stripe.api_key`; that needs `pip install 'agent-once[stripe]'`.

Verify the PaymentIntent, not the charge. Charges made by a PaymentIntent don't copy its metadata, so a `"charge"` lookup only finds charges you tagged yourself.

Use both helpers together. If a lookup misses, the call runs again with the same `Idempotency-Key`, and Stripe replays the first response instead of charging twice. Stripe keeps idempotency keys for at least 24 hours.

`examples/stripe_example.py` runs the whole flow offline against a fake client.

## HTTP

`http_verifier` asks your own API whether the effect happened:

```python
from once.verifiers import http_verifier

verify = http_verifier(
    "https://api.example.com/orders/{order_id}/emails/{key}",
    headers={"Authorization": f"Bearer {token}"},
)
```

```python
http_verifier(
    url,
    *,
    method="GET",
    headers=None,
    happened=range(200, 300),
    not_happened=(404,),
    timeout=10.0,
    result=None,
)
```

- `url` is a template. `{key}` is the once key and any other `{name}` is the tool argument of that name, each percent-encoded. Strings go in as they are and other values as JSON (`null`, `true`, `3`). A tool argument named `key` raises `ValueError` when the template has `{key}`; rename the argument.
- A status in `happened` returns the body: parsed JSON, the text if it is not JSON, or None if it is empty.
- A status in `not_happened` returns `NOT_HAPPENED`, so the call runs again.
- Any other status, or a network error, raises, which leaves the record unknown.
- `result` maps the body to what the tool returns. It may also return `NOT_HAPPENED`, for APIs that answer 200 with something like `{"found": false}`.
- A status in both `happened` and `not_happened` raises `ValueError`.

## Writing your own

A verifier is any `verify(key, args)` callable. Return the result if the effect happened, `NOT_HAPPENED` if it did not, and raise if you can't tell. For async tools it may be an async function.

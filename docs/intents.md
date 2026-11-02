# Intents

By default a replay only counts as the same call if every argument is equal. Agents re-plan, though: a retry may round an amount, reword a memo, or send an id as a number instead of a string. With plain keys that is a new key, and the side effect runs again.

`key=` fixes that by ignoring everything but one value. An intent is the general form. It answers two questions separately:

- **Which record is this?** The `id` fields, after normalizing, pick the key.
- **Is this the same act?** Every other field has a matcher that decides whether the new value is close enough to the stored one to replay.

```python
from once import Once, ignore, intent, within

once = Once()

REFUND = intent(
    "refund",
    id={"order_id": str},  # identity, with a normalizer
    match={
        "amount": within(pct=1),  # 500 then 499: same act
        "memo": ignore,  # any memo, not stored
    },
)


@once.tool(intent=REFUND)
def refund(order_id: str, amount: int, currency: str = "usd", memo: str = "") -> str:
    return stripe.Refund.create(...).id


refund("o1", 500, memo="damaged")  # runs
refund("o1", 499, memo="damaged item")  # replays the first result
refund("o1", 450)  # KeyConflict: amount: stored 500, got 450 (within(pct=1))
refund("o1", 500, currency="eur")  # KeyConflict: currency: stored 'usd', got 'eur' (exact)
```

## Identity

`id` is a list of parameter names, or a dict of name to normalizer. A normalizer turns a value into its canonical form, such as `str`, `str.lower` or `lambda s: s.strip().casefold()`. Normalizers should be idempotent (`n(n(x)) == n(x)`), and their output must be JSON. A `None` id value is an error.

The key is `make_key("refund@1", {"id": {"order_id": "o1"}}, scope)`. It comes from the intent's name, not the function's, so renaming or moving the function keeps its records, and two lambdas can't share keys by accident. Scopes and a caller's `idempotency_key` work as they do for any tool.

`version` (default 1) is part of the key. Bump it when you change an intent in a way that shouldn't replay records made under the old one.

## Matchers

| matcher | same when |
|---|---|
| `exact` | equal as JSON. The default for every field the intent doesn't name. |
| `ignore` | always. The field isn't stored, so it doesn't need to be JSON. |
| `within(abs=..., pct=...)` | numbers at most `abs` apart, or within `pct` percent of the stored value. With both, either is enough. Non-numbers must be equal. |
| `equal_after(fn)` | `fn(stored) == fn(new)`, such as `equal_after(str.strip)`. |
| any `(stored, new) -> bool` | whatever it returns. Subclass `Matcher` for a readable name in errors. |

Fields you don't mention are compared exactly, so forgetting one is safe: the worst case is a `KeyConflict`, never a wrong replay.

Matchers should be pure functions. A safety layer has to give the same answer every time, so don't match by meaning with a model or embeddings. If you need fuzzy text matching, write it into a normalizer or matcher you can test.

## Always compared with the first call

The first call's compared arguments are stored with the record (`once show` prints them as `args`) and never updated. Each replay is compared with them, not with the previous replay, so tolerances can't add up: with `within(pct=1)`, 500 then 504 replays and 504 then 509 would too, but 500 then 509 doesn't, and that's what you get.

When a ttl runs out, the next call runs again and its arguments become the new first call.

## Verify

A verify function with a parameter named `stored` gets the first call's compared arguments, as well as the current call's arguments in `args`:

```python
def find_refund(key: str, args: dict, stored: dict | None) -> object: ...
```

Verifiers without a `stored` parameter are called as before.

## Rules

- `intent` replaces `name` and `key`, so it can't be combined with them. `exclude` still works, and the intent can't name an excluded parameter or `idempotency_key`.
- The framework helpers take `intent=` too. The context parameter they leave out of the key can't be named by the intent.
- Tools without an intent are unchanged: same keys, same exact comparison.
- A custom `Store` needs an `args` parameter on `claim` and `renew` for tools with an intent. Without it, plain tools keep working and intent tools raise `TypeError`.

from __future__ import annotations

import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from once import canonical_json, make_key

# Strings that tend to break naive concatenation.
tricky = st.text(alphabet=st.sampled_from('ab{}[]",:\\\x00\x1f\u00e9\ud800'), max_size=6)
names = st.one_of(tricky, st.text(max_size=10))
scopes = st.one_of(st.none(), names)
scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    names,
)
values = st.recursive(
    scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(names, inner, max_size=4)),
    max_leaves=10,
)
arg_dicts = st.dictionaries(names, values, max_size=4)


@given(values)
def test_canonical_json_is_deterministic_and_round_trips(value: Any) -> None:
    text = canonical_json(value)
    assert text == canonical_json(value)
    assert text.isascii()
    assert canonical_json(json.loads(text)) == text


@given(st.dictionaries(names, values, max_size=6))
def test_canonical_json_ignores_key_order(d: dict[str, Any]) -> None:
    reversed_d = dict(reversed(list(d.items())))
    assert canonical_json(d) == canonical_json(reversed_d)


@given(names, arg_dicts, scopes)
def test_make_key_is_deterministic_and_order_independent(
    tool: str, args: dict[str, Any], scope: str | None
) -> None:
    key = make_key(tool, args, scope)
    assert key == make_key(tool, dict(reversed(list(args.items()))), scope)
    assert len(key) == 64
    int(key, 16)


@given(names, arg_dicts, scopes, names, arg_dicts, scopes)
def test_distinct_inputs_give_distinct_keys(
    t1: str,
    a1: dict[str, Any],
    s1: str | None,
    t2: str,
    a2: dict[str, Any],
    s2: str | None,
) -> None:
    same = t1 == t2 and canonical_json(a1) == canonical_json(a2) and s1 == s2
    assert (make_key(t1, a1, s1) == make_key(t2, a2, s2)) == same


@given(names, names, scopes)
def test_moving_text_between_parts_changes_the_key(head: str, tail: str, scope: str | None) -> None:
    # "ab" + "c" vs "a" + "bc" style shifts between tool name and scope
    whole = head + tail
    if tail:
        assert make_key(whole, {}, scope) != make_key(head, {}, tail + (scope or ""))
    assert make_key(head, {}, None) != make_key(head, {}, "")


def test_known_separator_cases() -> None:
    # all of these collided when the parts were concatenated
    assert make_key("t", {}, "{}x") != make_key("t{}", {}, "x")
    assert make_key("", {}, "{}") != make_key("{}", {}, None)

"""Intents: what makes two calls the same act.

A plain tool replays only when every argument is equal. An intent splits that
into two questions:

- which record is this? The ``id`` fields, after normalizing, pick the key.
- is it the same act? Each other field has a matcher, a pure function
  ``(stored, new) -> bool``. Fields the intent doesn't name must be equal.

    @once.tool(intent=intent(
        "refund",
        id={"order_id": str},
        match={"amount": within(pct=1), "memo": ignore},
    ))
    def refund(order_id: str, amount: int, memo: str = "") -> str: ...

A replay is always compared with the arguments of the first call, which are
stored with the record and never updated, so tolerances can't add up.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .key import canonical_json

Normalizer = Callable[[Any], Any]
"""Turns an id value into its canonical form, such as ``str`` or ``str.lower``."""


class Matcher:
    """Decides whether a new value is the same as the stored one.

    ``stored`` is the first call's value after a trip through JSON, so a
    tuple comes back as a list. Matchers must be pure: the same two values
    always give the same answer.
    """

    def __call__(self, stored: Any, new: Any) -> bool:
        raise NotImplementedError

    def __repr__(self) -> str:
        return type(self).__name__


def _same(a: Any, b: Any) -> bool:
    try:
        return canonical_json(a) == canonical_json(b)
    except (TypeError, ValueError):
        return False


class _Exact(Matcher):
    def __call__(self, stored: Any, new: Any) -> bool:
        return _same(stored, new)

    def __repr__(self) -> str:
        return "exact"


class _Ignore(Matcher):
    def __call__(self, stored: Any, new: Any) -> bool:
        return True

    def __repr__(self) -> str:
        return "ignore"


exact: Matcher = _Exact()
"""Equal as JSON. The default for fields an intent doesn't name."""

ignore: Matcher = _Ignore()
"""Any value matches. Ignored fields are not stored and need not be JSON."""


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True, repr=False)
class within(Matcher):
    """Numbers at most ``abs`` apart, or ``pct`` percent of the stored value.

    With both, either is enough. Values that are not numbers must be equal.
    """

    abs: float | None = None
    pct: float | None = None

    def __post_init__(self) -> None:
        if self.abs is None and self.pct is None:
            raise ValueError("within needs abs=, pct=, or both")
        for name in ("abs", "pct"):
            value = getattr(self, name)
            if value is not None and (not _number(value) or not value >= 0):
                raise ValueError(f"within({name}=...) must be a number, 0 or more")

    def __call__(self, stored: Any, new: Any) -> bool:
        if not (_number(stored) and _number(new)):
            return _same(stored, new)
        if not (math.isfinite(stored) and math.isfinite(new)):
            return False
        allowed = max(
            self.abs or 0.0,
            0.0 if self.pct is None else self.pct / 100 * abs(stored),
        )
        return bool(abs(new - stored) <= allowed)

    def __repr__(self) -> str:
        parts = [
            f"{n}={getattr(self, n)!r}" for n in ("abs", "pct") if getattr(self, n) is not None
        ]
        return f"within({', '.join(parts)})"


@dataclass(frozen=True, repr=False)
class equal_after(Matcher):
    """Equal once both values go through ``fn``, such as ``str.strip``."""

    fn: Callable[[Any], Any]

    def __call__(self, stored: Any, new: Any) -> bool:
        try:
            return _same(self.fn(stored), self.fn(new))
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"equal_after({getattr(self.fn, '__qualname__', self.fn)!r})"


def _keep(value: Any) -> Any:
    return value


@dataclass(frozen=True)
class Intent:
    """What makes two calls to a tool the same act. Build one with ``intent()``."""

    name: str
    id: Mapping[str, Normalizer]
    match: Mapping[str, Callable[[Any, Any], bool]] = field(default_factory=dict)
    version: int = 1

    @property
    def tag(self) -> str:
        """The name the key is made from, ``name@version``."""
        return f"{self.name}@{self.version}"

    def check(self, params: Collection[str], exclude: Collection[str] = ()) -> None:
        """Raise ValueError if a field is not a usable parameter of the tool."""
        from .core import KEY_PARAM

        for what, names in (("id", self.id), ("match", self.match)):
            bad = [n for n in names if n not in params or n == KEY_PARAM or n in exclude]
            if bad:
                listed = ", ".join(repr(n) for n in bad)
                raise ValueError(
                    f"intent {self.name!r}: {what} names {listed}, which are not parameters"
                    " that go into the key"
                )

    def identity(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The normalized id fields."""
        out: dict[str, Any] = {}
        for name, normalize in self.id.items():
            try:
                value = normalize(args[name])
            except Exception as exc:
                raise ValueError(
                    f"intent {self.name!r}: normalizing {name!r} raised {exc!r}"
                ) from exc
            if value is None:
                raise ValueError(f"intent {self.name!r}: id field {name!r} is None")
            out[name] = value
        try:
            canonical_json(out)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"intent {self.name!r}: id fields must be JSON after normalizing"
            ) from exc
        return out

    def compared(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """The fields stored with the record and checked on a replay."""
        return {
            k: v
            for k, v in args.items()
            if k not in self.id and self.match.get(k, exact) is not ignore
        }

    def differences(self, stored: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
        """Each field that doesn't match, as ``"name: stored x, got y (matcher)"``."""
        out = []
        for name in sorted(set(stored) | set(new)):
            matcher = self.match.get(name, exact)
            if name not in stored or name not in new:
                where = "the stored call" if name not in stored else "this call"
                out.append(f"{name}: missing from {where}")
                continue
            try:
                same = bool(matcher(stored[name], new[name]))
            except Exception as exc:
                raise ValueError(
                    f"intent {self.name!r}: matcher for {name!r} raised {exc!r}"
                ) from exc
            if not same:
                out.append(f"{name}: stored {stored[name]!r}, got {new[name]!r} ({matcher!r})")
        return out


def intent(
    name: str,
    *,
    id: Iterable[str] | Mapping[str, Normalizer | None],
    match: Mapping[str, Callable[[Any, Any], bool]] | None = None,
    version: int = 1,
) -> Intent:
    """Describe what makes two calls the same act.

    name: a stable name for the act. The key is made from it, not the
        function's name, so renaming or moving the function keeps its keys.
    id: the fields that say which act this is, as a list of names or a dict
        of name to normalizer (``{"order_id": str}``). Normalizers must be
        idempotent: ``n(n(x)) == n(x)``.
    match: matchers for other fields: ``exact``, ``ignore``, ``within(...)``,
        ``equal_after(fn)``, or any ``(stored, new) -> bool`` function.
        Fields not named here must be equal.
    version: bump it when you change the intent in a way that should not
        replay records made under the old one.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("intent name must be a non-empty str")
    if "@" in name:
        raise ValueError(f"intent name {name!r} can't contain '@'")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"intent version must be an int, 1 or more, got {version!r}")
    if isinstance(id, str):
        raise TypeError("id takes a list of field names, not a str")
    ids: dict[str, Normalizer] = (
        {k: v or _keep for k, v in id.items()}
        if isinstance(id, Mapping)
        else dict.fromkeys(id, _keep)
    )
    if not ids:
        raise ValueError(f"intent {name!r} needs at least one id field")
    for k, v in ids.items():
        if not callable(v):
            raise TypeError(f"intent {name!r}: normalizer for {k!r} is not callable")
    matchers = dict(match or {})
    both = sorted(set(ids) & set(matchers))
    if both:
        raise ValueError(f"intent {name!r}: {', '.join(map(repr, both))} in both id and match")
    for k, m in matchers.items():
        if not callable(m):
            raise TypeError(f"intent {name!r}: matcher for {k!r} is not callable")
    return Intent(name, ids, matchers, version)

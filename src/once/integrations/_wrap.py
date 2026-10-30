"""Shared wrapper: run a tool through Once with a scope taken from its arguments."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Collection
from typing import Any, ParamSpec, TypeVar, cast

from ..core import KEY_PARAM, TTL, KeyFn, Once, Verifier

P = ParamSpec("P")
R = TypeVar("R")

ScopeOf = Callable[[inspect.BoundArguments], "str | None"]


def wrap(
    once: Once,
    fn: Callable[P, R],
    *,
    name: str | None,
    timeout: float | None,
    verify: Verifier | None,
    exclude: Collection[str],
    scope_of: ScopeOf,
    ttl: TTL | None = None,
    key: KeyFn | None = None,
) -> Callable[P, R]:
    """Wrap ``fn`` with once.tool and set the scope from each call's arguments.

    If ``scope_of`` returns None, the scope from an enclosing ``once.scope()``
    (if any) is used. With ``key``, the excluded parameters are left out of
    the dict it gets.
    """
    inner: Callable[..., Any] = once._decorator(name, timeout, verify, exclude, ttl, key)(fn)
    sig = inspect.signature(fn)
    # Hide idempotency_key from the framework so it is not offered to the model.
    public = sig.replace(parameters=[p for p in sig.parameters.values() if p.name != KEY_PARAM])

    def scope_for(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
        return scope_of(sig.bind_partial(*args, **kwargs))

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            scope = scope_for(args, kwargs)
            if scope is None:
                return await inner(*args, **kwargs)
            with once.scope(scope):
                return await inner(*args, **kwargs)

        awrapper.__once_tool__ = inner.__once_tool__  # type: ignore[attr-defined]
        awrapper.__signature__ = public  # type: ignore[attr-defined]
        return cast(Callable[P, R], awrapper)

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        scope = scope_for(args, kwargs)
        if scope is None:
            return cast(R, inner(*args, **kwargs))
        with once.scope(scope):
            return cast(R, inner(*args, **kwargs))

    wrapper.__once_tool__ = inner.__once_tool__  # type: ignore[attr-defined]
    wrapper.__signature__ = public  # type: ignore[attr-defined]
    return wrapper


def find_param(sig: inspect.Signature, names: Collection[str], type_name: str) -> str | None:
    """The parameter annotated with ``type_name``, else the first one named in ``names``."""
    for param in sig.parameters.values():
        annotation = param.annotation
        text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
        if type_name in str(text):
            return param.name
    for param in sig.parameters.values():
        if param.name in names:
            return param.name
    return None

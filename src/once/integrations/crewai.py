"""Run CrewAI tools through once.

It does not import ``crewai``. Put it under ``@tool``:

    from once.integrations.crewai import once_tool

    @tool("charge_card")
    @once_tool(once, name="charge_card", scope_arg="order_id")
    def charge_card(order_id: str, amount: int) -> str: ...

CrewAI wants a docstring on the function. Its tools get no run context, so
the scope is the value of ``scope_arg`` if you name one, otherwise the
enclosing ``once.scope()``. Wrap ``crew.kickoff()`` in
``with once.scope(run_id):``; CrewAI copies context variables into its worker
threads, so the tools see it.

For a ``BaseTool`` subclass, decorate ``_run`` (or ``_arun``) and pass a name.
``self`` is left out of the key.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, overload

from ..core import TTL, Once, Verifier
from ._wrap import wrap

P = ParamSpec("P")
R = TypeVar("R")


@overload
def once_tool(once: Once, fn: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def once_tool(
    once: Once,
    /,
    *,
    name: str | None = None,
    timeout: float | None = None,
    ttl: TTL | None = None,
    verify: Verifier | None = None,
    scope_arg: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def once_tool(
    once: Once,
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    timeout: float | None = None,
    ttl: TTL | None = None,
    verify: Verifier | None = None,
    scope_arg: str | None = None,
) -> Any:
    """Decorate a CrewAI tool function so each distinct call runs at most once.

    scope_arg: a tool parameter whose value is the scope (an order or run id
        the model passes in). It stays part of the key too.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        params = list(inspect.signature(func).parameters)

        def scope_of(bound: inspect.BoundArguments) -> str | None:
            if scope_arg is not None and bound.arguments.get(scope_arg) is not None:
                return str(bound.arguments[scope_arg])
            return None

        exclude = ("self",) if params[:1] == ["self"] else ()
        return wrap(
            once,
            func,
            name=name,
            timeout=timeout,
            ttl=ttl,
            verify=verify,
            exclude=exclude,
            scope_of=scope_of,
        )

    if fn is not None:
        return decorate(fn)
    return decorate

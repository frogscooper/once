"""Run Pydantic AI tools through once.

It does not import ``pydantic_ai``. Put it under ``@agent.tool`` (or pass the
function in ``Agent(tools=[...])``); Pydantic AI passes ``RunContext`` as the
first parameter and hides it from the model.

    from once.integrations.pydantic_ai import once_tool

    @agent.tool
    @once_tool(once, scope_arg="order_id")
    async def refund(ctx: RunContext[Deps], order_id: str, amount: int) -> str: ...

The RunContext parameter is left out of the key. The scope is, in order: the
value of ``scope_arg`` if you name one, ``scope_from(ctx)`` if given, the
enclosing ``once.scope()``, then ``ctx.conversation_id`` (or ``ctx.run_id``
with ``scope="run"``). Pass your own ``conversation_id`` to ``agent.run``:
Pydantic AI makes up a fresh one for every run that doesn't, and then each
run gets its own key. ``run_id`` is stable when a durable workflow replays a
run. Tool call ids and retry counts are not used.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Literal, ParamSpec, TypeVar, overload

from ..core import TTL, Once, Verifier, current_scope
from ._wrap import find_param, wrap

P = ParamSpec("P")
R = TypeVar("R")

ScopeFrom = Callable[[Any], "str | None"]
RunScope = Literal["conversation", "run"]


def context_scope(ctx: Any, scope: RunScope) -> str | None:
    """``ctx.conversation_id`` or ``ctx.run_id`` from a RunContext."""
    value = getattr(ctx, "conversation_id" if scope == "conversation" else "run_id", None)
    return str(value) if value else None


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
    scope_from: ScopeFrom | None = None,
    scope: RunScope = "conversation",
    context_arg: str | None = None,
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
    scope_from: ScopeFrom | None = None,
    scope: RunScope = "conversation",
    context_arg: str | None = None,
) -> Any:
    """Decorate a Pydantic AI tool function so each distinct call runs at most once.

    scope_arg: a tool parameter whose value is the scope. It stays part of the
        key too.
    scope_from: called with the RunContext, returns the scope or None, e.g.
        ``lambda ctx: ctx.deps.user_id``.
    scope: "conversation" (the default) or "run": the RunContext id used when
        nothing above it gives a scope.
    context_arg: the RunContext parameter, if it is not annotated as
        RunContext or named ``ctx`` or ``context``.
    """
    if scope not in ("conversation", "run"):
        raise ValueError(f"scope must be 'conversation' or 'run', not {scope!r}")

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        ctx_name = context_arg or find_param(sig, ("ctx", "context"), "RunContext")

        def scope_of(bound: inspect.BoundArguments) -> str | None:
            if scope_arg is not None and bound.arguments.get(scope_arg) is not None:
                return str(bound.arguments[scope_arg])
            ctx = bound.arguments.get(ctx_name) if ctx_name else None
            if ctx is None:
                return None
            if scope_from is not None:
                value = scope_from(ctx)
                if value is not None:
                    return value
            enclosing = current_scope()
            return enclosing if enclosing is not None else context_scope(ctx, scope)

        exclude = (ctx_name,) if ctx_name else ()
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

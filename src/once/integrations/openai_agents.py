"""Run OpenAI Agents SDK function tools through once.

It does not import ``agents``. Put it under ``@function_tool``; the SDK passes
a ``RunContextWrapper`` or ``ToolContext`` as the first parameter and hides it
from the model.

    from once.integrations.openai_agents import once_tool

    @function_tool
    @once_tool(once, name="charge_card")
    async def charge_card(ctx: ToolContext[Deps], amount: int, customer: str) -> str: ...

The context parameter is left out of the key. The scope is, in order: the
value of ``scope_arg`` if you name one, ``scope_from(ctx)`` if given, the run's
``RunConfig.group_id`` (set it to your conversation or session id), or the
enclosing ``once.scope()``. Tool call ids are not used, because a retried call
gets a new one.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, overload

from ..core import Once, Verifier
from ._wrap import find_param, wrap

P = ParamSpec("P")
R = TypeVar("R")

ScopeFrom = Callable[[Any], "str | None"]


def group_id(ctx: Any) -> str | None:
    """``RunConfig.group_id`` for a ToolContext, or None if it is not set."""
    run_config: Any = getattr(ctx, "run_config", None)
    if isinstance(run_config, dict):
        value = run_config.get("group_id")
    else:
        value = getattr(run_config, "group_id", None)
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
    verify: Verifier | None = None,
    scope_arg: str | None = None,
    scope_from: ScopeFrom | None = None,
    context_arg: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def once_tool(
    once: Once,
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    timeout: float | None = None,
    verify: Verifier | None = None,
    scope_arg: str | None = None,
    scope_from: ScopeFrom | None = None,
    context_arg: str | None = None,
) -> Any:
    """Decorate an Agents SDK tool function so each distinct call runs at most once.

    scope_arg: a tool parameter whose value is the scope. It stays part of the
        key too.
    scope_from: called with the context wrapper, returns the scope or None,
        e.g. ``lambda ctx: ctx.context.user_id``.
    context_arg: the context parameter, if it is not annotated as
        RunContextWrapper or ToolContext or named ``ctx``, ``context`` or
        ``wrapper``.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        ctx_name = context_arg or find_param(sig, ("ctx", "context", "wrapper"), "Context")

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
            return group_id(ctx)

        exclude = (ctx_name,) if ctx_name else ()
        return wrap(
            once,
            func,
            name=name,
            timeout=timeout,
            verify=verify,
            exclude=exclude,
            scope_of=scope_of,
        )

    if fn is not None:
        return decorate(fn)
    return decorate

"""Run MCP server tools through once.

Works with the official ``mcp`` SDK's ``FastMCP`` (1.x) and ``MCPServer``
(2.x). It does not import ``mcp``; it only reads the Context object the server
passes in.

    from once.integrations.mcp import once_tool

    @server.tool()
    @once_tool(once, name="charge_card")
    async def charge_card(amount: int, customer: str, ctx: Context) -> dict: ...

The Context parameter is left out of the key. The scope is, in order: the
value of ``scope_arg`` if you name one, the MCP session id (the
``mcp-session-id`` header on streamable HTTP), or the enclosing
``once.scope()``. Request ids are not used, because a retried call is a new
request with a new id.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, overload

from ..core import TTL, Once, Verifier
from ._wrap import find_param, wrap

P = ParamSpec("P")
R = TypeVar("R")

SESSION_HEADER = "mcp-session-id"


def session_id(ctx: Any) -> str | None:
    """The MCP session id for a Context, or None outside a session."""
    if ctx is None:
        return None
    headers: Any = None
    try:
        headers = getattr(ctx, "headers", None)  # mcp 2.x
        if headers is None:
            request = getattr(ctx.request_context, "request", None)  # mcp 1.x
            headers = getattr(request, "headers", None)
    except Exception:  # no active request
        return None
    if headers is None:
        return None
    value = headers.get(SESSION_HEADER)
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
    context_arg: str | None = None,
) -> Any:
    """Decorate an MCP tool function so each distinct call runs at most once.

    scope_arg: a tool parameter whose value is the scope (an order or run id
        the model passes in). It stays part of the key too.
    context_arg: the Context parameter, if it is not annotated as Context or
        named ``ctx`` or ``context``.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        ctx_name = context_arg or find_param(sig, ("ctx", "context"), "Context")

        def scope_of(bound: inspect.BoundArguments) -> str | None:
            if scope_arg is not None and bound.arguments.get(scope_arg) is not None:
                return str(bound.arguments[scope_arg])
            return session_id(bound.arguments.get(ctx_name)) if ctx_name else None

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

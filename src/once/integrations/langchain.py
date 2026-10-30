"""Run LangChain / LangGraph tools through once.

It does not import langchain. Put it under ``@tool`` and give the function a
``config: RunnableConfig`` parameter; LangChain fills it in and hides it from
the model.

    from once.integrations.langchain import once_tool

    @tool
    @once_tool(once)
    def send_email(to: str, subject: str, config: RunnableConfig) -> str: ...

The config parameter is left out of the key. The scope is
``config["configurable"]["thread_id"]`` (change it with ``scope_key``), so a
LangGraph thread that replays a step after a crash or interrupt gets the
stored result. Without a thread id, the enclosing ``once.scope()`` applies.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar, overload

from ..core import TTL, KeyFn, Once, Verifier
from ._wrap import find_param, wrap

P = ParamSpec("P")
R = TypeVar("R")


def config_scope(config: Any, scope_key: str = "thread_id") -> str | None:
    """Read ``configurable[scope_key]`` from a RunnableConfig (a dict)."""
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    value = configurable.get(scope_key)
    return str(value) if value is not None else None


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
    key: KeyFn | None = None,
    verify: Verifier | None = None,
    scope_key: str = "thread_id",
    config_arg: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def once_tool(
    once: Once,
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    timeout: float | None = None,
    ttl: TTL | None = None,
    key: KeyFn | None = None,
    verify: Verifier | None = None,
    scope_key: str = "thread_id",
    config_arg: str | None = None,
) -> Any:
    """Decorate a LangChain tool function so each distinct call runs at most once.

    config_arg: the RunnableConfig parameter, if it is not annotated as
        RunnableConfig or named ``config``.
    key: picks the identity of a call, as in ``once.tool``. It doesn't get
        the config parameter.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(func)
        config_name = config_arg or find_param(sig, ("config",), "RunnableConfig")

        def scope_of(bound: inspect.BoundArguments) -> str | None:
            if config_name is None:
                return None
            return config_scope(bound.arguments.get(config_name), scope_key)

        exclude = (config_name,) if config_name else ()
        return wrap(
            once,
            func,
            name=name,
            timeout=timeout,
            ttl=ttl,
            key=key,
            verify=verify,
            exclude=exclude,
            scope_of=scope_of,
        )

    if fn is not None:
        return decorate(fn)
    return decorate

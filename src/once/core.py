"""The Once runner: claim a key, run the side effect, store the outcome."""

from __future__ import annotations

import functools
import inspect
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, overload

from .exceptions import EffectFailed, EffectPending, EffectUnknown, KeyConflict
from .key import args_hash, canonical_json, make_key
from .store import Record, SQLiteStore, Status

P = ParamSpec("P")
R = TypeVar("R")

KEY_PARAM = "idempotency_key"

_current_scope: ContextVar[str | None] = ContextVar("once_scope", default=None)


class _TimedOut(Exception):
    pass


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _call_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run fn in a daemon thread and stop waiting after ``timeout`` seconds.

    Python cannot kill the thread, so on timeout the call keeps running in the
    background and its outcome is discarded.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name="once-timeout")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise _TimedOut(f"no response after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


class Once:
    """Runs side-effecting calls at most once per key, backed by SQLite.

    path: SQLite file. Defaults to ./once.db.
    wait: seconds to wait for a pending record (owned by another process) to
        finish before raising EffectPending. 0 means raise right away.
    unknown_on: exception types that mean "the effect may have happened". They
        mark the record unknown instead of failed. Defaults to TimeoutError.
    busy_timeout: SQLite busy timeout in seconds.
    """

    def __init__(
        self,
        path: str | Path = "once.db",
        *,
        wait: float = 0.0,
        poll_interval: float = 0.05,
        unknown_on: tuple[type[BaseException], ...] = (TimeoutError,),
        busy_timeout: float = 5.0,
    ) -> None:
        self.store = SQLiteStore(path, busy_timeout=busy_timeout)
        self.wait = wait
        self.poll_interval = poll_interval
        self.unknown_on = unknown_on
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        self._locks_guard = threading.Lock()

    # public API

    @contextmanager
    def scope(self, scope: str | None) -> Iterator[None]:
        """Set the scope (run, order, or step id) for calls made inside the block."""
        token = _current_scope.set(scope)
        try:
            yield
        finally:
            _current_scope.reset(token)

    @overload
    def tool(self, fn: Callable[P, R]) -> Callable[P, R]: ...

    @overload
    def tool(
        self, *, name: str | None = None, timeout: float | None = None
    ) -> Callable[[Callable[P, R]], Callable[P, R]]: ...

    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Decorate a tool function so each distinct call runs at most once.

        The key is make_key(name, args, scope), where scope comes from
        ``with once.scope(...)``. If the function has an ``idempotency_key``
        parameter, the computed key is passed in. If the caller passes
        ``idempotency_key`` explicitly, that value is used as the key.
        """

        def decorate(func: Callable[P, R]) -> Callable[P, R]:
            if inspect.iscoroutinefunction(func):
                raise TypeError("once does not support async functions yet")
            tool_name = name or func.__name__

            @functools.wraps(func)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                result: R = self._run(func, tool_name, args, kwargs, None, None, timeout)
                return result

            wrapper.__once_tool__ = (func, tool_name)  # type: ignore[attr-defined]
            return wrapper

        if fn is not None:
            return decorate(fn)
        return decorate

    def execute(
        self,
        fn: Callable[..., Any],
        *,
        key: str | None = None,
        tool_name: str | None = None,
        scope: str | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call ``fn(**kwargs)`` at most once for ``key``.

        Without ``key``, the key is make_key(tool_name, kwargs, scope). ``fn`` may
        be a plain function or one decorated with ``tool``.
        """
        func: Callable[..., Any] = fn
        name = tool_name
        marker = getattr(fn, "__once_tool__", None)
        if marker is not None:
            func, default_name = marker
            name = name or default_name
        resolved = str(name or getattr(func, "__name__", "tool"))
        return self._run(func, resolved, (), kwargs, key, scope, timeout)

    def get(self, key: str) -> Record | None:
        """Return the stored record for ``key``, if any."""
        return self.store.get(key)

    def reset(self, key: str) -> bool:
        """Delete the record for ``key`` so the next call runs again.

        Use this after you have checked by hand that a failed or unknown effect
        did not happen (or that you want it to happen again).
        """
        return self.store.delete(key)

    # internals

    @contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        with self._locks_guard:
            lock, count = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, count + 1)
        try:
            with lock:
                yield
        finally:
            with self._locks_guard:
                lock, count = self._locks[key]
                if count <= 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, count - 1)

    def _run(
        self,
        func: Callable[..., Any],
        tool_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        key: str | None,
        scope: str | None,
        timeout: float | None,
    ) -> Any:
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        explicit = bound.arguments.pop(KEY_PARAM, None)
        call_args = dict(bound.arguments)
        try:
            canonical_json(call_args)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"arguments to {tool_name!r} must be JSON serializable") from exc

        if key is None:
            key = explicit
        if key is None:
            if scope is None:
                scope = _current_scope.get()
            key = make_key(tool_name, call_args, scope)
        if KEY_PARAM in sig.parameters:
            bound.arguments[KEY_PARAM] = key
        h = args_hash({"tool": tool_name, "args": call_args})

        with self._key_lock(key):
            if self.store.claim(key, h):
                return self._run_owned(key, lambda: func(*bound.args, **bound.kwargs), timeout)
            return self._existing(key, h)

    def _existing(self, key: str, h: str) -> Any:
        record = self.store.get(key)
        deadline = time.monotonic() + self.wait
        while (
            record is not None and record.status is Status.PENDING and time.monotonic() < deadline
        ):
            time.sleep(self.poll_interval)
            record = self.store.get(key)
        if record is None:
            raise EffectPending(Record(key, Status.PENDING, None, "record was deleted", h, "", ""))
        if record.args_hash != h:
            raise KeyConflict(key, record.args_hash, h)
        if record.status is Status.DONE:
            return json.loads(record.result) if record.result is not None else None
        if record.status is Status.PENDING:
            raise EffectPending(record)
        if record.status is Status.FAILED:
            raise EffectFailed(record)
        raise EffectUnknown(record)

    def _run_owned(self, key: str, call: Callable[[], Any], timeout: float | None) -> Any:
        try:
            result = call() if timeout is None else _call_with_timeout(call, timeout)
        except _TimedOut as exc:
            self.store.finish(key, Status.UNKNOWN, error=f"timeout: {exc}")
            raise EffectUnknown(self._record(key)) from None
        except self.unknown_on as exc:
            self.store.finish(key, Status.UNKNOWN, error=_describe(exc))
            raise
        except Exception as exc:
            self.store.finish(key, Status.FAILED, error=_describe(exc))
            raise
        except BaseException as exc:
            # KeyboardInterrupt and friends: we cannot tell if the effect happened.
            self.store.finish(key, Status.UNKNOWN, error=_describe(exc))
            raise

        try:
            payload = json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            self.store.finish(key, Status.UNKNOWN, error=f"result is not JSON serializable: {exc}")
            raise EffectUnknown(self._record(key)) from exc
        self.store.finish(key, Status.DONE, result=payload)
        return json.loads(payload)

    def _record(self, key: str) -> Record:
        record = self.store.get(key)
        assert record is not None
        return record

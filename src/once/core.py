"""The Once runner: claim a key, run the side effect, store the outcome."""

from __future__ import annotations

import asyncio
import contextvars
import enum
import functools
import inspect
import json
import math
import threading
import time
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast, overload

from ._otel import aspan, span
from .exceptions import EffectFailed, EffectPending, EffectUnknown, KeyConflict
from .hooks import Event, Hook, Hooks, Kind, Outcome, acall_hook, call_sync_hook, logger
from .key import args_hash, canonical_json, make_key
from .store import Record, Status, Store, expiry, open_store

P = ParamSpec("P")
R = TypeVar("R")

KEY_PARAM = "idempotency_key"

Verifier = Callable[[str, dict[str, Any]], Any]
"""verify(key, args) returns the effect's result if it happened, or NOT_HAPPENED.

For async tools it may also be an async function.
"""

TTL = float | timedelta
"""Seconds (int or float) or a timedelta. math.inf means never expire."""

_current_scope: ContextVar[str | None] = ContextVar("once_scope", default=None)


def current_scope() -> str | None:
    """The scope set by the innermost ``once.scope(...)`` block, or None."""
    return _current_scope.get()


class _NotHappened(enum.Enum):
    NOT_HAPPENED = "not_happened"


NOT_HAPPENED = _NotHappened.NOT_HAPPENED
"""Return this from a verify callback when the side effect did not happen."""

# Rounds of "claim, read, verify" before giving up when other callers keep
# changing the record under us.
_MAX_ROUNDS = 5

_NOT_STARTED = "interrupted before the call started"


class _TimedOut(Exception):
    pass


@dataclass(frozen=True)
class _InFlight:
    """A call this process is running right now."""

    done: threading.Event
    thread: int
    is_async: bool


@dataclass(frozen=True)
class _Call:
    key: str
    tool_name: str
    args_hash: str
    args: dict[str, Any]
    invoke: Callable[[], Any]


@dataclass
class _Trace:
    """What one execute did, for hooks and spans."""

    key: str
    tool_name: str
    started_at: float = field(default_factory=time.time)
    t0: float = field(default_factory=time.monotonic)
    outcome: Outcome | None = None
    status: Status | None = None
    waited: bool = False
    started: bool = False  # the tool function was called

    def event(self, kind: Kind, record: Record | None, error: BaseException | None) -> Event:
        status = self.status if self.status is not None else Status.PENDING
        duration = time.monotonic() - self.t0
        return Event(
            kind,
            self.key,
            self.tool_name,
            status,
            self.outcome or "ran",
            self.started_at,
            duration,
            record,
            error,
        )


def _ttl(ttl: TTL | None) -> float | None:
    if ttl is None:
        return None
    seconds = ttl.total_seconds() if isinstance(ttl, timedelta) else float(ttl)
    if math.isnan(seconds) or seconds <= 0:
        raise ValueError(f"ttl must be positive, got {ttl!r}")
    try:
        expiry(seconds)
    except OverflowError:
        return math.inf  # past year 9999, so it never runs out
    return seconds


def _expiry(ttl: float | None) -> str | None:
    return None if ttl is None or math.isinf(ttl) else expiry(ttl)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _call_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run fn in a daemon thread and stop waiting after ``timeout`` seconds.

    Python cannot kill the thread, so on timeout the call keeps running in the
    background and its outcome is discarded. fn runs in a copy of the
    caller's context, so once.scope and tracing still apply inside it.
    """
    box: dict[str, Any] = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["result"] = ctx.run(fn)
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
    """Runs side-effecting calls at most once per key.

    path: SQLite file, or a postgresql:// URL to use PostgresStore. Defaults
        to ./once.db.
    store: a store object to use instead of path, such as
        PostgresStore(dsn, table="..."). Pass path or store, not both.
    wait: seconds to wait for a pending record (owned by another process) to
        finish before raising EffectPending. 0 means raise right away.
    unknown_on: exception types that mean "the effect may have happened". They
        mark the record unknown instead of failed. Defaults to TimeoutError.
    stale_after: seconds after which a pending record counts as abandoned and
        is handed to verify, if the call has one. None means never.
    busy_timeout: SQLite busy timeout in seconds.
    ttl: default time to live for done records, in seconds or as a timedelta.
        Once it runs out, the next call with the same key runs again. Tools and
        calls can set their own. None (the default) means records never expire.
    hooks: a Hooks object with callbacks for claims, outcomes and replays.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        store: Store | None = None,
        wait: float = 0.0,
        poll_interval: float = 0.05,
        unknown_on: tuple[type[BaseException], ...] = (TimeoutError,),
        stale_after: float | None = None,
        busy_timeout: float = 5.0,
        ttl: TTL | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        if store is not None and path is not None:
            raise TypeError("pass path or store, not both")
        if store is None:
            store = open_store("once.db" if path is None else path, busy_timeout)
        self.store: Store = store
        self.wait = wait
        self.poll_interval = poll_interval
        self.unknown_on = unknown_on
        self.stale_after = stale_after
        self.ttl = _ttl(ttl)
        self.hooks = hooks
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        self._alocks: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}
        self._locks_guard = threading.Lock()
        self._inflight: dict[str, _InFlight] = {}
        self._inflight_guard = threading.Lock()

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
        self,
        *,
        name: str | None = None,
        timeout: float | None = None,
        verify: Verifier | None = None,
        exclude: Collection[str] = (),
        ttl: TTL | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]: ...

    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        timeout: float | None = None,
        verify: Verifier | None = None,
        exclude: Collection[str] = (),
        ttl: TTL | None = None,
    ) -> Any:
        """Decorate a tool function so each distinct call runs at most once.

        Works on plain and async functions; an async function gets an async
        wrapper. The key is make_key(name, args, scope), where scope comes from
        ``with once.scope(...)``. If the function has an ``idempotency_key``
        parameter, the computed key is passed in. If the caller passes a
        non-empty ``idempotency_key``, that value is used as the key; "" and
        None mean "compute one".

        ``exclude`` names parameters to leave out of the key, such as a
        framework context object. They are still passed to the function.

        ``ttl`` (seconds or a timedelta) makes a done result expire, after which
        the same call runs again. It overrides the Once default.
        """
        skip = frozenset(exclude)
        seconds = _ttl(ttl)

        def decorate(func: Callable[P, R]) -> Callable[P, R]:
            tool_name = name or func.__name__

            if inspect.iscoroutinefunction(func):

                @functools.wraps(func)
                async def awrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                    call = self._prepare(func, tool_name, args, kwargs, None, None, skip)
                    return await self._arun(call, True, timeout, verify, self._pick(seconds))

                awrapper.__once_tool__ = (func, tool_name, verify, skip, seconds)  # type: ignore[attr-defined]
                return cast(Callable[P, R], awrapper)

            @functools.wraps(func)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                call = self._prepare(func, tool_name, args, kwargs, None, None, skip)
                result: R = self._run(call, timeout, verify, self._pick(seconds))
                return result

            wrapper.__once_tool__ = (func, tool_name, verify, skip, seconds)  # type: ignore[attr-defined]
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
        verify: Verifier | None = None,
        ttl: TTL | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call ``fn(**kwargs)`` at most once for ``key``.

        Without ``key``, the key is make_key(tool_name, kwargs, scope). ``fn`` may
        be a plain function or one decorated with ``tool``, in which case its
        name, verify and ttl are used unless given here. Use aexecute for async
        functions.
        """
        func, name, verify, skip, seconds = self._unwrap(fn, tool_name, verify, ttl)
        if inspect.iscoroutinefunction(func):
            raise TypeError(f"{name!r} is async, use aexecute")
        call = self._prepare(func, name, (), kwargs, key, scope, skip)
        return self._run(call, timeout, verify, seconds)

    async def aexecute(
        self,
        fn: Callable[..., Any],
        *,
        key: str | None = None,
        tool_name: str | None = None,
        scope: str | None = None,
        timeout: float | None = None,
        verify: Verifier | None = None,
        ttl: TTL | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async version of execute.

        ``fn`` may be async or plain; a plain function runs in a worker thread.
        ``verify`` may be async or plain.
        """
        func, name, verify, skip, seconds = self._unwrap(fn, tool_name, verify, ttl)
        call = self._prepare(func, name, (), kwargs, key, scope, skip)
        is_async = inspect.iscoroutinefunction(func)
        return await self._arun(call, is_async, timeout, verify, seconds)

    def close(self) -> None:
        """Close the store's connections. The instance reconnects if used again."""
        self.store.close()

    def __enter__(self) -> Once:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, key: str) -> Record | None:
        """Return the stored record for ``key``, if any."""
        return self.store.get(key)

    def records(
        self, status: Status | str | Iterable[Status | str] | None = None, limit: int | None = None
    ) -> list[Record]:
        """Stored records, most recently updated first, optionally filtered by status."""
        return self.store.records(_statuses(status), limit)

    def purge(
        self,
        older_than: float | None = None,
        status: Status | str | Iterable[Status | str] = Status.DONE,
        *,
        expired: bool = False,
    ) -> int:
        """Delete old records. Returns the count.

        ``older_than`` deletes records not updated for that many seconds, only
        done ones by default. Purging pending or unknown records lets those
        calls run again, so pass them explicitly if that is what you want.
        ``expired=True`` deletes done records whose ttl has run out. Pass
        either or both.
        """
        if older_than is None and not expired:
            raise TypeError("pass older_than, expired=True, or both")
        count = self.store.purge_expired() if expired else 0
        if older_than is not None:
            statuses = _statuses(status)
            assert statuses is not None
            count += self.store.purge(older_than, statuses)
        return count

    def reset(self, key: str) -> bool:
        """Delete the record for ``key`` so the next call runs again.

        Use this after you have checked by hand that a failed or unknown effect
        did not happen (or that you want it to happen again).
        """
        return self.store.delete(key)

    # shared internals

    def _unwrap(
        self,
        fn: Callable[..., Any],
        tool_name: str | None,
        verify: Verifier | None,
        ttl: TTL | None,
    ) -> tuple[Callable[..., Any], str, Verifier | None, frozenset[str], float | None]:
        func: Callable[..., Any] = fn
        name = tool_name
        seconds = _ttl(ttl)
        marker = getattr(fn, "__once_tool__", None)
        if marker is not None:
            func, default_name, default_verify, skip, default_ttl = marker
            name = name or default_name
            verify = verify or default_verify
            seconds = default_ttl if seconds is None else seconds
        else:
            skip = frozenset()
        name = str(name or getattr(func, "__name__", "tool"))
        return func, name, verify, skip, self._pick(seconds)

    def _pick(self, ttl: float | None) -> float | None:
        return self.ttl if ttl is None else ttl

    def _prepare(
        self,
        func: Callable[..., Any],
        tool_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        key: str | None,
        scope: str | None,
        exclude: frozenset[str] = frozenset(),
    ) -> _Call:
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        explicit = bound.arguments.pop(KEY_PARAM, None)
        if explicit == "":  # often a default; same as None, so compute the key
            explicit = None
        call_args = {k: v for k, v in bound.arguments.items() if k not in exclude}
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

        def invoke() -> Any:
            return func(*bound.args, **bound.kwargs)

        return _Call(key, tool_name, h, call_args, invoke)

    def _verifiable(self, record: Record) -> bool:
        if record.status is Status.UNKNOWN:
            return True
        return (
            record.status is Status.PENDING
            and self.stale_after is not None
            and record.age() >= self.stale_after
        )

    def _error_status(self, exc: BaseException) -> Status:
        # KeyboardInterrupt, CancelledError and friends: we cannot tell if the
        # effect happened.
        if isinstance(exc, self.unknown_on) or not isinstance(exc, Exception):
            return Status.UNKNOWN
        return Status.FAILED

    # Calls in this process that own a key are registered in _inflight, so a
    # caller on the other path (sync vs async, or another loop or thread) that
    # finds the record pending can wait for it instead of raising EffectPending.
    # Claiming and registering happen under one lock so a loser never misses
    # the owner's entry.

    def _claim(
        self, key: str, h: str, owner: int | None = None
    ) -> tuple[str | None, _InFlight | None]:
        """Claim the key. ``owner`` is the event loop's thread for async callers."""
        with self._inflight_guard:
            token = self.store.claim(key, h)
            if token is not None:
                self._register(key, owner)
                return token, None
            return None, self._inflight.get(key)

    def _take_pending(self, record: Record, owner: int | None = None) -> str | None:
        with self._inflight_guard:
            token = self.store.take(record, Status.PENDING)
            if token is not None:
                self._register(record.key, owner)
            return token

    def _renew(self, record: Record, h: str, owner: int | None = None) -> str | None:
        """Claim an expired record. Only one caller wins, the rest see it pending."""
        with self._inflight_guard:
            token = self.store.renew(record, h)
            if token is not None:
                self._register(record.key, owner)
            return token

    def _register(self, key: str, owner: int | None) -> None:
        is_async = owner is not None
        thread = owner if owner is not None else threading.get_ident()
        self._inflight[key] = _InFlight(threading.Event(), thread, is_async)

    def _release(self, key: str) -> None:
        with self._inflight_guard:
            entry = self._inflight.pop(key, None)
        if entry is not None:
            entry.done.set()

    def _finish(
        self,
        key: str,
        token: str,
        status: Status,
        result: str | None = None,
        error: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Store the outcome, retrying once if the store errors.

        By now the effect has run, so a dropped connection should not leave
        the record pending. finish is fenced by the token, so a retry after a
        write that did land changes nothing.
        """
        try:
            self.store.finish(key, token, status, result, error, expires_at)
        except Exception:
            logger.warning("once: storing the outcome of %s failed, retrying", key, exc_info=True)
            self.store.finish(key, token, status, result, error, expires_at)

    def _give_back(self, key: str, token: str) -> None:
        """Undo a claim whose call never started, so the next caller runs it.

        finish fences off the record first, so this never deletes a claim
        someone else took over in the meantime.
        """
        try:
            if self.store.finish(key, token, Status.FAILED, error=_NOT_STARTED):
                record = self.store.get(key)
                if record and record.status is Status.FAILED and record.error == _NOT_STARTED:
                    self.store.delete(key)
        except Exception:
            logger.exception("once: could not give back the claim on %s", key)
        finally:
            self._release(key)

    def _record(self, key: str) -> Record:
        record = self.store.get(key)
        assert record is not None
        return record

    # hooks

    def _hook(self, kind: Kind) -> Hook | None:
        return None if self.hooks is None else self.hooks.get(kind)

    def _emit(
        self,
        kind: Kind,
        trace: _Trace,
        record: Record | None = None,
        error: BaseException | None = None,
    ) -> None:
        hook = self._hook(kind)
        if hook is None:
            return
        try:
            if record is None:
                record = self.store.get(trace.key)
            event = trace.event(kind, record, error)
        except Exception:
            logger.exception("once: could not build the on_%s event for %s", kind, trace.key)
            return
        call_sync_hook(hook, event)

    async def _aemit(
        self,
        kind: Kind,
        trace: _Trace,
        record: Record | None = None,
        error: BaseException | None = None,
    ) -> None:
        hook = self._hook(kind)
        if hook is None:
            return
        try:
            if record is None:
                record = await asyncio.to_thread(self.store.get, trace.key)
            event = trace.event(kind, record, error)
        except Exception:
            logger.exception("once: could not build the on_%s event for %s", kind, trace.key)
            return
        await acall_hook(hook, event)

    def _finished(self, trace: _Trace) -> Kind | None:
        """The hook for this call's own outcome, if it stored one."""
        if trace.status is Status.UNKNOWN:
            trace.outcome = "unknown"
        if trace.status is None or trace.status is Status.PENDING:
            return None
        return cast(Kind, trace.status.value)

    def _settled(self, trace: _Trace, record: Record | None) -> None:
        """Note how a call that could not run or replay ended."""
        if record is None or record.status is Status.PENDING:
            trace.outcome = "pending"
        elif record.status is Status.UNKNOWN:
            trace.outcome = "unknown"
        else:
            trace.outcome = "replayed"
        if record is not None:
            trace.status = record.status

    # sync path

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
        self, call: _Call, timeout: float | None, verify: Verifier | None, ttl: float | None
    ) -> Any:
        key, h = call.key, call.args_hash
        trace = _Trace(key, call.tool_name)
        with span(trace), self._key_lock(key):
            record: Record | None = None
            for _ in range(_MAX_ROUNDS):
                token, local = self._claim(key, h)
                if token is not None:
                    return self._run_owned(trace, token, call.invoke, timeout, ttl)
                if local is not None and not (
                    local.is_async and local.thread == threading.get_ident()
                ):
                    # Owned by an async call in this process. Waiting is only
                    # safe if that call is not running on our own thread.
                    local.done.wait()
                    trace.waited = True
                    continue
                record = self._read_settled(trace)
                if record is None:
                    continue  # deleted between claim and read
                if record.expired():
                    # As if it had been purged, so the args may differ too.
                    token = self._renew(record, h)
                    if token is not None:
                        trace.outcome = "expired-rerun"
                        return self._run_owned(trace, token, call.invoke, timeout, ttl)
                    continue
                if record.args_hash != h:
                    trace.outcome = "conflict"
                    raise KeyConflict(key, record.args_hash, h)
                if record.status is Status.DONE:
                    trace.outcome = "waited" if trace.waited else "replayed"
                    trace.status = Status.DONE
                    self._emit("replay", trace, record)
                    return _loads(record.result)
                if verify is None or not self._verifiable(record):
                    break
                outcome = verify(key, dict(call.args))
                if outcome is NOT_HAPPENED:
                    token = self._take_pending(record)
                    if token is not None:
                        return self._run_owned(trace, token, call.invoke, timeout, ttl)
                    continue
                payload = _dumps(outcome)
                done = self.store.take(record, Status.DONE, payload, expires_at=_expiry(ttl))
                if done is not None:
                    trace.outcome, trace.status = "verified", Status.DONE
                    self._emit("done", trace)
                    return _loads(payload)
            self._settled(trace, record)
            raise _error_for(key, h, record)

    def _read_settled(self, trace: _Trace) -> Record | None:
        """Read the record, waiting up to self.wait while it is pending."""
        record = self.store.get(trace.key)
        deadline = time.monotonic() + self.wait
        while (
            record is not None and record.status is Status.PENDING and time.monotonic() < deadline
        ):
            time.sleep(self.poll_interval)
            trace.waited = True
            record = self.store.get(trace.key)
        return record

    def _run_owned(
        self,
        trace: _Trace,
        token: str,
        call: Callable[[], Any],
        timeout: float | None,
        ttl: float | None,
    ) -> Any:
        trace.outcome = trace.outcome or "ran"
        error: BaseException | None = None
        try:
            self._emit("claim", trace)
            return self._run_owned_inner(trace, token, call, timeout, ttl)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if trace.started:
                self._release(trace.key)
            else:
                self._give_back(trace.key, token)
            kind = self._finished(trace)
            if kind is not None:
                self._emit(kind, trace, error=error)

    def _run_owned_inner(
        self,
        trace: _Trace,
        token: str,
        call: Callable[[], Any],
        timeout: float | None,
        ttl: float | None,
    ) -> Any:
        key = trace.key
        trace.started = True
        try:
            result = call() if timeout is None else _call_with_timeout(call, timeout)
        except _TimedOut as exc:
            self._finish(key, token, Status.UNKNOWN, error=f"timeout: {exc}")
            trace.status = Status.UNKNOWN
            raise EffectUnknown(self._record(key)) from None
        except BaseException as exc:
            status = self._error_status(exc)
            self._finish(key, token, status, error=_describe(exc))
            trace.status = status
            raise

        try:
            payload = _dumps(result)
        except TypeError as exc:
            self._finish(
                key, token, Status.UNKNOWN, error=f"result is not JSON serializable: {exc}"
            )
            trace.status = Status.UNKNOWN
            raise EffectUnknown(self._record(key)) from exc
        self._finish(key, token, Status.DONE, result=payload, expires_at=_expiry(ttl))
        trace.status = Status.DONE
        return _loads(payload)

    # async path
    #
    # Async callers do not take the threading lock. Coroutines on the same loop
    # are serialized by a per-loop, per-key asyncio.Lock; everything else (other
    # threads, loops, processes) is coordinated through the store claim, the
    # same way separate processes are. Store calls run in worker threads.

    @asynccontextmanager
    async def _key_alock(self, key: str) -> AsyncIterator[None]:
        slot = (id(asyncio.get_running_loop()), key)
        with self._locks_guard:
            lock, count = self._alocks.get(slot, (asyncio.Lock(), 0))
            self._alocks[slot] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            with self._locks_guard:
                lock, count = self._alocks[slot]
                if count <= 1:
                    del self._alocks[slot]
                else:
                    self._alocks[slot] = (lock, count - 1)

    async def _arun(
        self,
        call: _Call,
        is_async: bool,
        timeout: float | None,
        verify: Verifier | None,
        ttl: float | None,
    ) -> Any:
        key, h = call.key, call.args_hash
        trace = _Trace(key, call.tool_name)
        loop_thread = threading.get_ident()
        async with aspan(trace), self._key_alock(key):
            record: Record | None = None
            for _ in range(_MAX_ROUNDS):
                token, local = await self._astep(key, self._claim, key, h, loop_thread)
                if token is not None:
                    return await self._arun_owned(trace, token, call.invoke, is_async, timeout, ttl)
                if local is not None:
                    while not local.done.is_set():
                        await asyncio.sleep(self.poll_interval)
                    trace.waited = True
                    continue
                record = await self._aread_settled(trace)
                if record is None:
                    continue
                if record.expired():
                    token = await self._astep(key, self._renew, record, h, loop_thread)
                    if token is not None:
                        trace.outcome = "expired-rerun"
                        return await self._arun_owned(
                            trace, token, call.invoke, is_async, timeout, ttl
                        )
                    continue
                if record.args_hash != h:
                    trace.outcome = "conflict"
                    raise KeyConflict(key, record.args_hash, h)
                if record.status is Status.DONE:
                    trace.outcome = "waited" if trace.waited else "replayed"
                    trace.status = Status.DONE
                    await self._aemit("replay", trace, record)
                    return _loads(record.result)
                if verify is None or not self._verifiable(record):
                    break
                outcome = await _acall_verify(verify, key, dict(call.args))
                if outcome is NOT_HAPPENED:
                    token = await self._astep(key, self._take_pending, record, loop_thread)
                    if token is not None:
                        return await self._arun_owned(
                            trace, token, call.invoke, is_async, timeout, ttl
                        )
                    continue
                payload = _dumps(outcome)
                taken = await asyncio.to_thread(
                    self.store.take, record, Status.DONE, payload, None, _expiry(ttl)
                )
                if taken is not None:
                    trace.outcome, trace.status = "verified", Status.DONE
                    await self._aemit("done", trace)
                    return _loads(payload)
            self._settled(trace, record)
            raise _error_for(key, h, record)

    async def _astep(self, key: str, step: Callable[..., R], *args: Any) -> R:
        """Run a claiming store step in a worker thread.

        The thread cannot be stopped, so if we get cancelled it may still claim
        the key. In that case the claim is given back once the step finishes,
        even if the event loop is gone by then.
        """
        lock = threading.Lock()
        state: dict[str, Any] = {}

        def run() -> R:
            result = step(*args)
            with lock:
                state["result"] = result
                abandoned = "abandoned" in state
            if abandoned:
                self._abandon(key, result)
            return result

        try:
            return await asyncio.shield(asyncio.to_thread(run))
        except asyncio.CancelledError:
            with lock:
                state["abandoned"] = True
                finished = "result" in state
            if finished:
                self._abandon(key, state["result"])
            raise

    def _abandon(self, key: str, result: Any) -> None:
        token = result[0] if isinstance(result, tuple) else result
        if token is not None:
            threading.Thread(target=self._give_back, args=(key, token), daemon=True).start()

    async def _aread_settled(self, trace: _Trace) -> Record | None:
        record = await asyncio.to_thread(self.store.get, trace.key)
        deadline = time.monotonic() + self.wait
        while (
            record is not None and record.status is Status.PENDING and time.monotonic() < deadline
        ):
            await asyncio.sleep(self.poll_interval)
            trace.waited = True
            record = await asyncio.to_thread(self.store.get, trace.key)
        return record

    async def _afinish(
        self,
        key: str,
        token: str,
        status: Status,
        result: str | None = None,
        error: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        # Shielded so the outcome is still written if we get cancelled again.
        await asyncio.shield(
            asyncio.to_thread(self._finish, key, token, status, result, error, expires_at)
        )

    async def _arun_owned(
        self,
        trace: _Trace,
        token: str,
        invoke: Callable[[], Any],
        is_async: bool,
        timeout: float | None,
        ttl: float | None,
    ) -> Any:
        trace.outcome = trace.outcome or "ran"
        error: BaseException | None = None
        try:
            await self._aemit("claim", trace)
            return await self._arun_owned_inner(trace, token, invoke, is_async, timeout, ttl)
        except BaseException as exc:
            error = exc
            raise
        finally:
            if trace.started:
                self._release(trace.key)
            else:
                # Shielded so the claim is given back even if we get cancelled again.
                await asyncio.shield(asyncio.to_thread(self._give_back, trace.key, token))
            kind = self._finished(trace)
            if kind is not None:
                await self._aemit(kind, trace, error=error)

    async def _arun_owned_inner(
        self,
        trace: _Trace,
        token: str,
        invoke: Callable[[], Any],
        is_async: bool,
        timeout: float | None,
        ttl: float | None,
    ) -> Any:
        key = trace.key
        trace.started = True
        raised: list[BaseException] = []

        async def tracked() -> Any:
            try:
                if is_async:
                    return await invoke()
                return await asyncio.to_thread(invoke)
            except BaseException as exc:
                raised.append(exc)
                raise

        try:
            if timeout is None:
                result = await tracked()
            else:
                result = await asyncio.wait_for(tracked(), timeout)
        except BaseException as exc:
            timed_out = (
                timeout is not None
                and isinstance(exc, asyncio.TimeoutError)
                and not any(e is exc for e in raised)
            )
            if timed_out:
                await self._afinish(
                    key, token, Status.UNKNOWN, error=f"timeout: no response after {timeout}s"
                )
                trace.status = Status.UNKNOWN
                raise EffectUnknown(await asyncio.to_thread(self._record, key)) from None
            status = self._error_status(exc)
            await self._afinish(key, token, status, error=_describe(exc))
            trace.status = status
            raise

        try:
            payload = _dumps(result)
        except TypeError as exc:
            await self._afinish(
                key, token, Status.UNKNOWN, error=f"result is not JSON serializable: {exc}"
            )
            trace.status = Status.UNKNOWN
            raise EffectUnknown(await asyncio.to_thread(self._record, key)) from exc
        await self._afinish(key, token, Status.DONE, result=payload, expires_at=_expiry(ttl))
        trace.status = Status.DONE
        return _loads(payload)


async def _acall_verify(verify: Verifier, key: str, args: dict[str, Any]) -> Any:
    if inspect.iscoroutinefunction(verify):
        return await verify(key, args)
    outcome = await asyncio.to_thread(verify, key, args)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    return outcome


def _statuses(
    status: Status | str | Iterable[Status | str] | None,
) -> list[Status] | None:
    if status is None:
        return None
    if isinstance(status, str):  # Status is a str too
        return [Status(status)]
    return [Status(s) for s in status]


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(str(exc)) from exc


def _loads(payload: str | None) -> Any:
    return json.loads(payload) if payload is not None else None


def _error_for(key: str, h: str, record: Record | None) -> Exception:
    if record is None:
        return EffectPending(Record(key, Status.PENDING, None, "record keeps changing", h, "", ""))
    if record.status is Status.PENDING:
        return EffectPending(record)
    if record.status is Status.FAILED:
        return EffectFailed(record)
    return EffectUnknown(record)

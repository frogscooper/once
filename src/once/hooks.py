"""Lifecycle hooks: callbacks that see each claim, outcome and replay."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .store import Record, Status

logger = logging.getLogger("once")

Kind = Literal["claim", "done", "failed", "unknown", "replay"]

Outcome = Literal[
    "ran", "replayed", "waited", "expired-rerun", "verified", "unknown", "pending", "conflict"
]
"""How a call was settled.

ran: this call ran the function. expired-rerun: same, after a ttl ran out.
replayed: a stored result (or stored failure) was returned. waited: same,
after waiting for another caller to finish. verified: verify said the effect
happened and its result was stored. unknown: the outcome is unknown, so
EffectUnknown was raised. pending: another caller still holds the key.
conflict: the key was used with other arguments.
"""


@dataclass(frozen=True)
class Event:
    """What a hook gets.

    started_at is the wall clock time (time.time()) the call began, and
    duration the seconds since then. record is the stored record at the time
    of the event. error is the exception the function raised, for on_failed
    and on_unknown.
    """

    kind: Kind
    key: str
    tool_name: str
    status: Status
    outcome: Outcome
    started_at: float
    duration: float
    record: Record | None
    error: BaseException | None = None


Hook = Callable[[Event], object]


@dataclass(frozen=True)
class Hooks:
    """Callbacks for Once(hooks=...). Each is optional.

    on_claim: this call claimed the key and is about to run the function.
    on_done, on_failed, on_unknown: the function this call ran finished, and
        the record was stored with that status. on_done also fires when verify
        resolves a record as done.
    on_replay: a stored done result was returned without running anything.

    Hooks run inline, so keep them fast. In aexecute and async tools a hook
    may be async. An exception from a hook is logged to the "once" logger
    and otherwise ignored.
    """

    on_claim: Hook | None = None
    on_done: Hook | None = None
    on_failed: Hook | None = None
    on_unknown: Hook | None = None
    on_replay: Hook | None = None

    def get(self, kind: Kind) -> Hook | None:
        hook: Hook | None = getattr(self, f"on_{kind}")
        return hook


def call_hook(hook: Hook, event: Event) -> object:
    """Call a hook from sync code. Returns what it returned, or None if it raised."""
    try:
        out = hook(event)
    except Exception:
        logger.exception("once: on_%s hook failed for %s", event.kind, event.key)
        return None
    return out


def call_sync_hook(hook: Hook, event: Event) -> None:
    out = call_hook(hook, event)
    if inspect.iscoroutine(out):
        out.close()
        logger.warning("once: on_%s hook is async; use aexecute to run it", event.kind)


async def acall_hook(hook: Hook, event: Event) -> None:
    out = call_hook(hook, event)
    if inspect.isawaitable(out):
        try:
            await out
        except Exception:
            logger.exception("once: on_%s hook failed for %s", event.kind, event.key)

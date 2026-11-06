"""Make side-effecting agent tool calls safe to retry."""

from .core import NOT_HAPPENED, Once, Verifier, current_scope
from .exceptions import (
    EffectError,
    EffectFailed,
    EffectPending,
    EffectUnknown,
    KeyConflict,
    OnceError,
)
from .hooks import Event, Hooks
from .intent import Intent, Matcher, equal_after, exact, ignore, intent, within
from .key import args_hash, canonical_json, make_key
from .store import Record, SQLiteStore, Status, Store

__all__ = [
    "NOT_HAPPENED",
    "EffectError",
    "EffectFailed",
    "EffectPending",
    "EffectUnknown",
    "Event",
    "Hooks",
    "Intent",
    "KeyConflict",
    "Matcher",
    "Once",
    "OnceError",
    "Record",
    "SQLiteStore",
    "Status",
    "Store",
    "Verifier",
    "args_hash",
    "canonical_json",
    "current_scope",
    "equal_after",
    "exact",
    "ignore",
    "intent",
    "make_key",
    "within",
]

__version__ = "0.3.0"

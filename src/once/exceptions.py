"""Exceptions raised when a key already has a record that is not a stored result."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Record


class OnceError(Exception):
    """Base class for all errors raised by once."""


class KeyConflict(OnceError):
    """The key was already used with different arguments."""

    def __init__(
        self, key: str, stored_hash: str | None, new_hash: str, detail: str | None = None
    ) -> None:
        if detail is None:
            message = (
                f"key {key!r} was already used with different arguments "
                f"(stored args_hash {stored_hash}, got {new_hash})"
            )
        else:
            message = f"key {key!r} was already used with different arguments: {detail}"
        super().__init__(message)
        self.key = key
        self.detail = detail
        """For a tool with an intent, which fields didn't match and how."""


class EffectError(OnceError):
    """Base for errors that carry the stored record."""

    def __init__(self, record: Record, message: str) -> None:
        super().__init__(message)
        self.record = record
        self.key = record.key


class EffectPending(EffectError):
    """Another caller claimed the key and has not finished yet."""

    def __init__(self, record: Record) -> None:
        super().__init__(record, f"effect {record.key!r} is still pending")


class EffectFailed(EffectError):
    """A previous attempt failed with a known error. It will not be retried."""

    def __init__(self, record: Record) -> None:
        super().__init__(record, f"effect {record.key!r} failed earlier: {record.error}")


class EffectUnknown(EffectError):
    """A previous attempt may or may not have happened (timeout, crash, interrupt)."""

    def __init__(self, record: Record) -> None:
        super().__init__(record, f"effect {record.key!r} has unknown outcome: {record.error}")

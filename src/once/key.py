"""Deterministic keys for tool calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to JSON with sorted keys and no extra whitespace."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def args_hash(args: Mapping[str, Any]) -> str:
    """Hash of the call arguments, used to detect key reuse with different args."""
    return hashlib.sha256(canonical_json(dict(args)).encode()).hexdigest()


def make_key(tool_name: str, args: Mapping[str, Any], scope: str | None = None) -> str:
    """Build an idempotency key from a tool name, its arguments, and an optional scope.

    The key is sha256(tool_name + canonical_json(args) + scope), as hex. The
    scope is usually a run, order, or step id. The same tool called with the
    same arguments in a different scope gets a different key.
    """
    payload = tool_name + canonical_json(dict(args)) + (scope or "")
    return hashlib.sha256(payload.encode()).hexdigest()

"""Deterministic keys for tool calls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to JSON with sorted keys, no extra whitespace, ASCII only."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def args_hash(args: Mapping[str, Any]) -> str:
    """Hash of the call arguments, used to detect key reuse with different args."""
    return hashlib.sha256(canonical_json(dict(args)).encode()).hexdigest()


def make_key(tool_name: str, args: Mapping[str, Any], scope: str | None = None) -> str:
    """Build an idempotency key from a tool name, its arguments, and an optional scope.

    The key is sha256(canonical_json([tool_name, args, scope])), as hex. Encoding
    the three parts as one JSON array keeps them apart, so no choice of tool
    name, arguments, or scope can collide with another. The scope is usually a
    run, order, or step id; None and "" are different scopes.
    """
    payload = canonical_json([tool_name, dict(args), scope])
    return hashlib.sha256(payload.encode()).hexdigest()

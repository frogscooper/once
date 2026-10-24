"""Regression tests for bugs found before launch."""

from __future__ import annotations

from pathlib import Path

from once import Once


def test_timeout_keeps_scope(db: str | Path) -> None:
    once = Once(db)
    seen: list[str | None] = []

    @once.tool
    def inner(n: int) -> int:
        return n

    @once.tool(timeout=5)
    def outer(n: int) -> int:
        from once import current_scope

        seen.append(current_scope())
        return inner(n)

    with once.scope("order-1"):
        outer(1)
    assert seen == ["order-1"]
    # inner ran under the order-1 scope, so another order does not replay it
    with once.scope("order-2"):
        assert once.execute(inner, n=1) == 1
    assert len(once.records()) == 3
    once.close()

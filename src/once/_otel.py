"""OpenTelemetry spans for execute and aexecute.

opentelemetry is imported on the first call, not when once is imported, and
only if it is installed (the agent-once[otel] extra, or pip install
opentelemetry-api). Without a configured TracerProvider the API hands out no-op spans.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import _Trace

_UNSET: Any = object()
_tracer: Any = _UNSET


def tracer() -> Any:
    """The "once" tracer, or None if opentelemetry is not installed."""
    global _tracer
    if _tracer is _UNSET:
        try:
            from opentelemetry import trace
        except ImportError:
            _tracer = None
        else:
            from . import __version__

            _tracer = trace.get_tracer("once", __version__)
    return _tracer


@contextmanager
def span(trace: _Trace) -> Iterator[None]:
    """A span named "once <tool>" around one call. Exceptions are recorded on it."""
    tr = tracer()
    if tr is None:
        yield
        return
    attributes = {"once.key": trace.key, "once.tool": trace.tool_name}
    with tr.start_as_current_span(f"once {trace.tool_name}", attributes=attributes) as current:
        try:
            yield
        finally:
            if trace.outcome is not None:
                current.set_attribute("once.outcome", trace.outcome)
            if trace.status is not None:
                current.set_attribute("once.status", trace.status.value)


@asynccontextmanager
async def aspan(trace: _Trace) -> AsyncIterator[None]:
    with span(trace):
        yield

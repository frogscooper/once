# Hooks and OpenTelemetry

## Hooks

Pass `Hooks` to see what happens to each call, for logging, metrics or a demo:

```python
from once import Event, Hooks, Once


def show(event: Event) -> None:
    print(event.kind, event.tool_name, event.outcome, f"{event.duration:.3f}s")


once = Once("once.db", hooks=Hooks(on_claim=show, on_done=show, on_replay=show))
```

Every hook is optional:

| hook | fires when |
|------|------------|
| `on_claim` | this call claimed the key and is about to run the function |
| `on_done` | the function finished and the result was stored, or `verify` resolved the record as done |
| `on_failed` | the function raised and the record was stored as failed |
| `on_unknown` | the record was stored as unknown |
| `on_replay` | a stored done result was returned without running anything |

Each hook gets an `Event`:

| field | |
|-------|---|
| `kind` | `"claim"`, `"done"`, `"failed"`, `"unknown"` or `"replay"` |
| `key` | the key |
| `tool_name` | the tool name |
| `status` | the record's `Status` |
| `outcome` | how the call was settled, see below |
| `started_at` | wall clock time the call began, from `time.time()` |
| `duration` | seconds since then |
| `record` | the stored `Record` at the time of the event |
| `error` | the exception, for `on_failed` and `on_unknown` |

Hooks run inline, so keep them fast. In `aexecute` and async tools a hook may be async; in sync calls an async hook is skipped with a warning. A hook that raises is logged to the `once` logger and the call carries on.

## Outcomes

| outcome | meaning |
|---------|---------|
| `ran` | this call ran the function |
| `expired-rerun` | same, after a ttl ran out |
| `replayed` | a stored result (or stored failure) was returned |
| `waited` | same, after waiting for another caller to finish |
| `verified` | `verify` said the effect happened and its result was stored |
| `unknown` | this call stored an unknown record, or found one |
| `pending` | another caller still holds the key, so `EffectPending` was raised |
| `conflict` | the key was used with other arguments |

## OpenTelemetry

```
pip install 'agent-once[otel]'
```

This adds `opentelemetry-api`. Each call through `execute`, `aexecute` or a decorated tool then makes a span named `once <tool_name>` with these attributes:

- `once.key`
- `once.tool`
- `once.status`
- `once.outcome`, one of the outcomes above

Exceptions are recorded on the span. Spans go wherever your `TracerProvider` sends them; with none configured the spans are no-ops. The tracer is named `once`. `import once` never imports OpenTelemetry; it is loaded on the first call, and only if it is installed.

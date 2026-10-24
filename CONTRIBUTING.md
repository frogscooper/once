# Contributing

Bug reports and pull requests are welcome. For anything bigger than a small fix, open an issue first so we can agree on the approach.

## Setup

```
python -m venv .venv
.venv/bin/pip install -e '.[dev,postgres,stripe]'
```

## Checks

CI runs these, so run them before opening a pull request:

```
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy src tests
.venv/bin/python -m pytest
```

The Postgres tests are skipped unless `ONCE_TEST_POSTGRES_DSN` points at a database they can create tables in, for example `postgresql://postgres:postgres@localhost:5432/once_test`. Slow stress tests are marked `heavy` and run with `pytest -m heavy`.

The docs site is MkDocs Material:

```
pip install -r requirements-docs.txt
mkdocs serve
```

## Pull requests

- Add a test for a bug fix or new behavior.
- Update the README, the docs and `CHANGELOG.md` when behavior changes.
- Keep changes to stored keys and the table schema backward compatible, or say clearly in the changelog that they are not.

# Live tests

These tests talk to the real Amazon Ads console and the real MySQL server. They
are **deselected by default** (`-m 'not live'` in `pyproject.toml`) so that
`uv run pytest` stays fast, offline and side-effect free.

```bash
uv run pytest -m live                                  # everything live
uv run pytest -m live tests/live/test_database_live.py # MySQL only
uv run pytest -m live tests/live/test_amazon_live.py   # Ads console only
```

Preconditions:

* `.env` is populated and points at the intended database.
* For the Amazon test, a session exists: run `uv run python run.py --login` once.

These tests never write to the table. They connect, reflect and navigate only.

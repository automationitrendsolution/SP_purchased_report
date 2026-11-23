"""Read-only checks against the real MySQL server in .env.  Never writes."""

from __future__ import annotations

import pytest

from app.config import PROJECT_ROOT, load_settings
from app.database import build_repository
from app.report_processor import load_heading_map

pytestmark = pytest.mark.live


@pytest.fixture
def live_repository(monkeypatch):
    # conftest disables .env loading for unit tests; live tests need it back.
    from dotenv import load_dotenv

    monkeypatch.setattr("app.config.load_dotenv", load_dotenv)
    monkeypatch.setenv("CREATE_TABLES", "false")
    settings = load_settings()
    mapping = load_heading_map(settings.heading_map_file)
    repo = build_repository(settings, mapping)
    yield settings, repo
    repo.dispose()


def test_connects_and_reflects_the_table(live_repository) -> None:
    settings, repo = live_repository
    spec = repo.spec()
    assert spec.name == settings.table
    assert {"report_date", "country", "advertised_asin", "purchased_asin"} <= set(spec.columns)


def test_scope_is_consistent_with_the_table(live_repository) -> None:
    settings, repo = live_repository
    repo.resolve_scope(settings.scope)  # raises if a scope column has no value


def test_shipped_heading_map_matches_the_live_table(live_repository) -> None:
    _settings, repo = live_repository
    assert repo.validate_schema() == []
    assert (PROJECT_ROOT / "config" / "Purchased_Product_Heading.csv").is_file()

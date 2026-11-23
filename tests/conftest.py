"""Shared fixtures.

Nothing here touches Amazon or a real MySQL server.  Database tests run against
in-memory SQLite using the very same table definition the bootstrap DDL
produces, so reflection and DELETE/INSERT/ROLLBACK behaviour are exercised for
real.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.pool import StaticPool

from app.config import PROJECT_ROOT, Settings, load_settings
from app.database import ReportRepository
from app.report_processor import load_heading_map
from app.schema import REQUIRED_SOURCE_COLUMNS, build_bootstrap_table

TABLE = "sponsored_products_purchased_product_report"

#: Minimum environment a valid Settings needs.
BASE_ENV = {
    "AMAZON_ENTITY_ID": "ENTITYTEST",
    "DB_HOST": "localhost",
    "DB_PORT": "3306",
    "DB_NAME": "test_db",
    "DB_USER": "tester",
    "DB_PASSWORD": "s3cret",
}

_PREFIXES = (
    "AMAZON_", "DB_", "DOWNLOAD_", "PROCESSED_", "FAILED_", "LOG_", "SCREENSHOT_", "SESSION_",
    "LOCK_", "HEADING_", "HEADLESS", "BROWSER_", "VIEWPORT_", "SLOW_MO", "REPORT_",
    "MAX_RETRIES", "RETRY_", "CREATE_TABLES", "FILE_RETENTION", "KEEP_PROCESSED", "BRAND_CODE",
)


@pytest.fixture(autouse=True)
def isolated_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate unit tests from the developer's real environment and ``.env``.

    Live tests are the exception: they exist to use the real configuration.
    """
    if request.node.get_closest_marker("live"):
        yield
        return
    monkeypatch.setattr("app.config.load_dotenv", lambda *_a, **_k: False)
    for key in list(os.environ):
        if key.startswith(_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(BASE_ENV)


@pytest.fixture
def settings(env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Fully valid settings pointed at a temporary directory tree."""
    for key, name in (
        ("DOWNLOAD_DIR", "downloads"),
        ("PROCESSED_DIR", "processed"),
        ("FAILED_DIR", "failed"),
        ("SCREENSHOT_DIR", "screenshots"),
        ("LOG_DIR", "logs"),
        ("SESSION_DIR", "session"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("LOCK_FILE", str(tmp_path / "pipeline.lock"))
    monkeypatch.setenv("RETRY_BACKOFF_SECONDS", "0")
    loaded = load_settings(env_file=None)
    loaded.ensure_directories()
    return loaded


@pytest.fixture
def mapping() -> dict[str, str]:
    return load_heading_map(PROJECT_ROOT / "config" / "Purchased_Product_Heading.csv")


def make_repository(mapping: dict[str, str], *, extra_columns=()) -> ReportRepository:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata = MetaData()
    table = build_bootstrap_table(mapping, table_name=TABLE, metadata=metadata)
    for column in extra_columns:
        table.append_column(column)
    metadata.create_all(engine)
    repo = ReportRepository(engine, table=TABLE, mapping=mapping)
    repo.reflect()
    return repo


@pytest.fixture
def repository(mapping: dict[str, str]) -> Iterator[ReportRepository]:
    repo = make_repository(mapping)
    try:
        yield repo
    finally:
        repo.dispose()


def report_row(
    day: date = date(2026, 11, 1),
    *,
    country: str = "United States",
    advertised: str = "B000ADV0001",
    purchased: str = "B000PUR0001",
    **overrides: object,
) -> dict[str, object]:
    """One report row with Amazon's headers and XLSX-style values."""
    row: dict[str, object] = {
        "Date": pd.Timestamp(day),
        "Portfolio name": "No Portfolio",
        "Campaign Name": "SP | 50% off | $10 tier",
        "Country": country,
        "Currency": "USD",
        "Ad Group Name": "Auto Group",
        "Retailer": "Amazon",
        "Advertised SKU": "SKU-01",
        "Advertised ASIN": advertised,
        "Targeting": 'category="Sunshades" price>12',
        "Match Type": "-",
        "Purchased ASIN": purchased,
        "7 Day Other SKU Units (#)": 2,
        "7 Day Other SKU Orders (#)": 1,
        "7 Day Other SKU Sales": 19.99,
    }
    row.update(overrides)
    return row


def write_xlsx(path: Path, rows: list[dict[str, object]], *, preamble: list[str] | None = None) -> Path:
    """Write rows the way Amazon's XLSX export lays them out."""
    frame = pd.DataFrame(rows, columns=list(REQUIRED_SOURCE_COLUMNS))
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        start = len(preamble or [])
        frame.to_excel(writer, index=False, startrow=start)
        if preamble:
            sheet = writer.sheets["Sheet1"]
            for i, line in enumerate(preamble, start=1):
                sheet.cell(row=i, column=1, value=line)
    return path

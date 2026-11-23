"""Transactions, scoping, and the no-delete-without-insert guarantee."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Column, String

from app.data_transformer import ReplaceSlice
from app.database import ReportRepository
from app.exceptions import DatabaseError
from tests.conftest import make_repository


def rec(day: date, country: str = "United States", **extra: object) -> dict[str, object]:
    return {
        "report_date": day,
        "country": country,
        "advertised_asin": "B1",
        "purchased_asin": "B2",
        "other_sku_units_7d": 1,
        **extra,
    }


def seed(repo: ReportRepository, records: list[dict[str, object]]) -> None:
    days = [r["report_date"] for r in records]
    repo.replace_slices(
        [ReplaceSlice(records[0]["country"], min(days), max(days), len(records))], records
    )


def test_reflection_excludes_the_generated_key(repository: ReportRepository) -> None:
    spec = repository.spec()
    assert "id" not in spec.columns
    assert "loaded_at" in spec.defaulted_columns
    assert {"other_sku_units_7d", "other_sku_orders_7d"} <= spec.integer_columns


def test_replaces_only_the_covered_dates_and_country(repository: ReportRepository) -> None:
    seed(repository, [rec(date(2026, 11, d)) for d in range(1, 11)])  # US 1..10
    seed(repository, [rec(date(2026, 11, 5), "United Kingdom")])

    result = repository.replace_slices(
        [ReplaceSlice("United States", date(2026, 11, 8), date(2026, 11, 12), 2)],
        [rec(date(2026, 11, 8)), rec(date(2026, 11, 12))],
    )
    assert (result.deleted, result.inserted) == (3, 2)  # 8, 9, 10 replaced
    assert repository.count_between(date(2026, 11, 1), date(2026, 11, 30), country="United States") == 9
    assert repository.count_between(date(2026, 11, 1), date(2026, 11, 30), country="United Kingdom") == 1


def test_rerunning_the_same_load_is_idempotent(repository: ReportRepository) -> None:
    rows = [rec(date(2026, 11, d)) for d in (1, 2, 3)]
    seed(repository, rows)
    seed(repository, rows)
    assert repository.count_between(date(2026, 11, 1), date(2026, 11, 3)) == 3


def test_failed_insert_rolls_back_the_delete(repository: ReportRepository) -> None:
    seed(repository, [rec(date(2026, 11, 1)), rec(date(2026, 11, 2))])
    with pytest.raises(DatabaseError):
        repository.replace_slices(
            [ReplaceSlice("United States", date(2026, 11, 1), date(2026, 11, 2), 1)],
            [rec(date(2026, 11, 1)), rec(date(2026, 11, 2), report_date=None)],  # NOT NULL
        )
    assert repository.count_between(date(2026, 11, 1), date(2026, 11, 2)) == 2


def test_refuses_to_delete_without_rows_to_insert(repository: ReportRepository) -> None:
    with pytest.raises(ValueError):
        repository.replace_slices(
            [ReplaceSlice("United States", date(2026, 11, 1), date(2026, 11, 2), 0)], []
        )


def test_scope_column_without_a_value_is_refused(mapping) -> None:
    repo = make_repository(mapping, extra_columns=[Column("brand_code", String(32))])
    with pytest.raises(DatabaseError, match="BRAND_CODE"):
        repo.resolve_scope({})
    assert repo.resolve_scope({"brand_code": "acme"}) == {"brand_code": "acme"}


def test_scoped_delete_leaves_other_brands_alone(mapping) -> None:
    repo = make_repository(mapping, extra_columns=[Column("brand_code", String(32))])
    day = date(2026, 11, 1)
    part = [ReplaceSlice("United States", day, day, 1)]
    repo.replace_slices(part, [rec(day, brand_code="acme")], scope={"brand_code": "acme"})
    repo.replace_slices(part, [rec(day, brand_code="other")], scope={"brand_code": "other"})
    repo.replace_slices(part, [rec(day, brand_code="acme")], scope={"brand_code": "acme"})
    assert repo.count_between(day, day, scope={"brand_code": "acme"}) == 1
    assert repo.count_between(day, day, scope={"brand_code": "other"}) == 1


def test_reflect_requires_the_key_columns(mapping) -> None:
    broken = dict(mapping, Country="country_name")
    repo = make_repository(mapping)
    bad = ReportRepository(repo.engine, table=repo.table_name, mapping=broken)
    with pytest.raises(DatabaseError, match="country_name"):
        bad.reflect()

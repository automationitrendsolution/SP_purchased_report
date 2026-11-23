"""Cleaning, typing, replace-slice computation and table alignment."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.data_transformer import (
    align_to_table,
    clean_numeric_value,
    parse_dates,
    replace_slices,
    to_records,
    transform,
)
from app.exceptions import DataTransformationError
from app.schema import REQUIRED_SOURCE_COLUMNS, TableSpec
from tests.conftest import report_row


def frame(*rows: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(REQUIRED_SOURCE_COLUMNS))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,204.55", 1204.55),
        ("12.5%", 12.5),
        ("(45.00)", -45.0),
        ("-", None),
        ("", None),
        (None, None),
        (7, 7.0),
        (float("inf"), None),
    ],
)
def test_clean_numeric_value(raw: object, expected: float | None) -> None:
    assert clean_numeric_value(raw) == expected


def test_parse_dates_accepts_amazon_formats() -> None:
    series = pd.Series([pd.Timestamp("2026-10-24"), "2026-10-25", "Oct 26, 2026", "10/27/2026"])
    assert parse_dates(series).tolist() == [
        date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26), date(2026, 10, 27)
    ]


def test_parse_dates_refuses_to_guess() -> None:
    with pytest.raises(DataTransformationError, match="could not be parsed"):
        parse_dates(pd.Series(["2026-10-24", "yesterday"]))


def test_slices_are_per_country_and_span_the_data() -> None:
    typed = frame(
        report_row(date(2026, 11, 1)),
        report_row(date(2026, 11, 5)),
        report_row(date(2026, 11, 3), country="United Kingdom"),
    )
    typed["Date"] = parse_dates(typed["Date"])
    slices = replace_slices(typed)
    assert [(s.country, s.start, s.end, s.rows) for s in slices] == [
        ("United Kingdom", date(2026, 11, 3), date(2026, 11, 3), 1),
        ("United States", date(2026, 11, 1), date(2026, 11, 5), 2),
    ]


def test_align_leaves_server_defaulted_columns_to_the_server() -> None:
    spec = TableSpec(
        name="t",
        columns=("report_date", "country", "extra", "loaded_at"),
        defaulted_columns=frozenset({"loaded_at"}),
    )
    aligned = align_to_table(pd.DataFrame({"report_date": [1], "country": ["US"], "x": [0]}), spec)
    assert list(aligned.columns) == ["report_date", "country", "extra"]
    assert aligned["extra"].isna().all()


def test_transform_types_and_renames(mapping, repository) -> None:
    result = transform(
        frame(report_row(**{"7 Day Other SKU Sales": "$1,204.555", "7 Day Other SKU Units (#)": "3"})),
        mapping,
        table=repository.spec(),
    )
    record = to_records(result.frame)[0]
    assert record["report_date"] == date(2026, 11, 1)
    assert record["campaign_name"] == "SP | 50% off | $10 tier"  # text untouched
    assert record["other_sku_units_7d"] == 3
    assert record["other_sku_sales_7d"] == pytest.approx(1204.56)
    assert record["match_type"] is None  # Amazon's "-" placeholder
    assert "loaded_at" not in record and "id" not in record


def test_transform_refuses_an_empty_frame(mapping, repository) -> None:
    with pytest.raises(DataTransformationError):
        transform(frame(), mapping, table=repository.spec())

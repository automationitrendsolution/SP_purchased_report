"""Reading .xlsx / .csv / .zip reports and the heading map."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import pytest

from app.exceptions import EmptyReportError, ReportValidationError
from app.report_processor import load_heading_map, read_report
from tests.conftest import report_row, write_xlsx


def test_reads_xlsx(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "r.xlsx", [report_row(), report_row(date(2026, 11, 2))])
    result = read_report(path)
    assert result.row_count == 2
    assert result.file_format == "xlsx"
    assert result.missing_columns == []


def test_finds_header_below_a_metadata_preamble(tmp_path: Path) -> None:
    path = write_xlsx(
        tmp_path / "r.xlsx", [report_row()], preamble=["Report: Purchased product", "Generated today"]
    )
    result = read_report(path)
    assert result.header_row == 2
    assert result.row_count == 1


def test_reads_csv_and_zip(tmp_path: Path) -> None:
    csv_text = (
        "Date,Portfolio name,Campaign Name,Country,Currency,Ad Group Name,Retailer,Advertised SKU,"
        "Advertised ASIN,Targeting,Match Type,Purchased ASIN,7 Day Other SKU Units (#),"
        "7 Day Other SKU Orders (#),7 Day Other SKU Sales\n"
        '2026-11-01,P,"Camp, with comma",United States,USD,G,Amazon,S,B1,t,-,B2,1,1,"$1,204.55"\n'
    )
    csv_path = tmp_path / "r.csv"
    csv_path.write_text(csv_text, encoding="utf-8-sig")
    assert read_report(csv_path).frame.loc[0, "Campaign Name"] == "Camp, with comma"

    zip_path = tmp_path / "r.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(csv_path, "inner/report.csv")
    assert read_report(zip_path).row_count == 1


def test_drops_blank_and_total_rows(tmp_path: Path) -> None:
    total = report_row()
    total["Date"] = "Total"
    path = write_xlsx(tmp_path / "r.xlsx", [report_row(), total])
    assert read_report(path).row_count == 1


def test_rejects_a_file_without_identity_columns(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text("Campaign,Spend\nA,1\n", encoding="utf-8")
    with pytest.raises(ReportValidationError):
        read_report(path)


def test_empty_report_is_its_own_error(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "r.xlsx", [])
    with pytest.raises(EmptyReportError):
        read_report(path)


def test_missing_optional_column_is_backfilled(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text(
        "Date,Country,Advertised ASIN,Purchased ASIN\n2026-11-01,United States,B1,B2\n",
        encoding="utf-8",
    )
    result = read_report(path)
    assert "7 Day Other SKU Sales" in result.missing_columns
    assert "7 Day Other SKU Sales" in result.frame.columns


def test_heading_map_rejects_two_sources_on_one_destination(tmp_path: Path) -> None:
    path = tmp_path / "map.csv"
    path.write_text("Old_Column_Name,New_Column_Name\nA,x\nB,x\n", encoding="utf-8")
    with pytest.raises(ReportValidationError):
        load_heading_map(path)


def test_shipped_heading_map_covers_every_report_column(mapping: dict[str, str]) -> None:
    from app.schema import REQUIRED_SOURCE_COLUMNS

    assert set(REQUIRED_SOURCE_COLUMNS) <= set(mapping)

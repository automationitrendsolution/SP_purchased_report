"""Reading and validating the downloaded report (.xlsx, .csv or a .zip of either).

Responsibilities stop at "this is a well-formed Purchased product report with
every column present".  Renaming, cleaning and typing happen in
:mod:`app.data_transformer`.
"""

from __future__ import annotations

import io
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .exceptions import EmptyReportError, ReportValidationError
from .logging_config import get_logger
from .schema import IDENTITY_COLUMNS, REQUIRED_SOURCE_COLUMNS

logger = get_logger("report")

#: Encodings tried in order for CSV.  Amazon exports UTF-8 (often with a BOM).
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
#: How many leading rows to scan when hunting for the real header row.
_HEADER_SCAN_ROWS = 20
_NA_VALUES = ["", "-", "--", "N/A", "n/a", "NA"]


@dataclass
class ReadResult:
    """A parsed report plus everything worth logging about it."""

    frame: pd.DataFrame
    source_path: Path
    file_format: str
    header_row: int
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.frame)


def load_heading_map(path: Path) -> dict[str, str]:
    """Load the Amazon-header -> MySQL-column mapping.

    Raises:
        ReportValidationError: If the file is missing, malformed, or maps one
            source column onto two destinations.
    """
    if not path.is_file():
        raise ReportValidationError(f"Column mapping file not found: {path}")
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ReportValidationError(f"Could not read column mapping {path}: {exc}") from exc

    columns = {c.strip().lower(): c for c in frame.columns}
    old_key = columns.get("old_column_name") or columns.get("old_column")
    new_key = columns.get("new_column_name") or columns.get("new_column")
    if not old_key or not new_key:
        raise ReportValidationError(
            f"{path} must have 'Old_Column_Name' and 'New_Column_Name' headers, "
            f"found: {list(frame.columns)}"
        )

    mapping: dict[str, str] = {}
    for old, new in zip(frame[old_key], frame[new_key], strict=True):
        old, new = old.strip(), new.strip()
        if not old or not new:
            continue
        if old in mapping and mapping[old] != new:
            raise ReportValidationError(
                f"{path} maps {old!r} onto both {mapping[old]!r} and {new!r}"
            )
        mapping[old] = new

    destinations = list(mapping.values())
    duplicates = sorted({d for d in destinations if destinations.count(d) > 1})
    if duplicates:
        raise ReportValidationError(f"{path} maps several columns onto {duplicates}")
    if not mapping:
        raise ReportValidationError(f"{path} contains no usable column mappings")
    logger.debug("Loaded %d column mappings from %s", len(mapping), path)
    return mapping


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _unpack(path: Path) -> tuple[io.BytesIO | Path, str]:
    """Return the readable payload and its format ('xlsx' or 'csv')."""
    suffix = path.suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                members = [
                    n for n in archive.namelist() if n.lower().endswith((".xlsx", ".csv"))
                ]
                if not members:
                    raise ReportValidationError(f"{path} contains no .xlsx or .csv file")
                if len(members) > 1:
                    logger.warning("%s holds %d reports; using %s", path, len(members), members[0])
                payload = io.BytesIO(archive.read(members[0]))
                return payload, Path(members[0]).suffix.lower().lstrip(".")
        except zipfile.BadZipFile as exc:
            raise ReportValidationError(f"{path} is not a valid zip: {exc}") from exc
    if suffix == ".xlsx":
        return path, "xlsx"
    if suffix == ".csv":
        return path, "csv"
    raise ReportValidationError(f"Unsupported report format {suffix!r} ({path})")


def _read_raw(payload: io.BytesIO | Path, file_format: str) -> pd.DataFrame:
    """Read every cell with no header, so the real header row can be located."""
    try:
        if file_format == "xlsx":
            # Amazon's workbooks carry no default cell style; openpyxl warns about
            # it on every read.  Harmless, but it buries real errors in cron output.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message="Workbook contains no default style", category=UserWarning
                )
                return pd.read_excel(payload, header=None, dtype=object, engine="openpyxl")
        last_error: Exception | None = None
        for encoding in _ENCODINGS:
            try:
                if isinstance(payload, io.BytesIO):
                    payload.seek(0)
                return pd.read_csv(
                    payload,
                    header=None,
                    dtype=str,
                    encoding=encoding,
                    sep=None,
                    engine="python",
                    keep_default_na=False,
                )
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ReportValidationError(f"Could not decode CSV with any of {_ENCODINGS}: {last_error}")
    except ReportValidationError:
        raise
    except Exception as exc:
        raise ReportValidationError(f"Could not parse report: {exc}") from exc


def _find_header_row(raw: pd.DataFrame) -> int:
    """Amazon sometimes prefixes exports with metadata lines; find the header."""
    needles = {c.lower() for c in IDENTITY_COLUMNS}
    for index in range(min(_HEADER_SCAN_ROWS, len(raw))):
        cells = {str(v).strip().lower() for v in raw.iloc[index].tolist() if pd.notna(v)}
        if len(needles & cells) >= 2:
            return index
    raise ReportValidationError(
        f"Could not find a header row in the first {_HEADER_SCAN_ROWS} rows; "
        f"expected columns like {IDENTITY_COLUMNS}"
    )


def read_report(path: Path) -> ReadResult:
    """Parse the report into a DataFrame with all required columns.

    Raises:
        ReportValidationError: If the file cannot be parsed as this report.
        EmptyReportError: If it parses but has no data rows.
    """
    if not path.is_file():
        raise ReportValidationError(f"Report file not found: {path}")

    payload, file_format = _unpack(path)
    raw = _read_raw(payload, file_format)
    if raw.empty:
        raise EmptyReportError(f"Report {path} is empty. Existing database rows were left untouched.")

    header_row = _find_header_row(raw)
    header = [str(v).strip() if pd.notna(v) else "" for v in raw.iloc[header_row].tolist()]
    frame = raw.iloc[header_row + 1 :].copy()
    frame.columns = header
    frame = frame.loc[:, [c for c in frame.columns if c and not c.startswith("Unnamed:")]]
    frame = frame.replace({v: pd.NA for v in _NA_VALUES})

    present = set(frame.columns)
    absent_identity = [c for c in IDENTITY_COLUMNS if c not in present]
    if absent_identity:
        raise ReportValidationError(
            f"{path.name} is missing required column(s) {absent_identity}; it is not a "
            "Purchased product report (or the report's columns were changed)."
        )

    missing = [c for c in REQUIRED_SOURCE_COLUMNS if c not in present]
    for column in missing:
        frame[column] = pd.NA
    if missing:
        logger.warning(
            "Report is missing %d expected column(s); created as NULL: %s",
            len(missing),
            ", ".join(missing),
        )
    extra = sorted(present.difference(REQUIRED_SOURCE_COLUMNS))
    if extra:
        logger.info("Report has %d extra column(s), not loaded: %s", len(extra), ", ".join(extra))

    frame = _drop_non_data_rows(frame)
    result = ReadResult(
        frame=frame,
        source_path=path,
        file_format=file_format,
        header_row=header_row,
        missing_columns=missing,
        extra_columns=extra,
    )
    logger.info(
        "Parsed %s: %d row(s), %d column(s), format=%s, header_row=%d",
        path.name,
        result.row_count,
        len(frame.columns),
        file_format,
        header_row,
    )
    if result.row_count == 0:
        raise EmptyReportError(
            f"Report {path} parsed successfully but contains zero data rows. "
            "Existing database rows were left untouched."
        )
    return result


def _drop_non_data_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove blank rows and trailing total/summary rows (no date)."""
    cleaned = frame.dropna(how="all")
    dates = cleaned["Date"].astype("string").str.strip()
    keep = dates.notna() & (dates != "") & ~dates.str.lower().isin({"total", "totals"})
    dropped = int((~keep).sum())
    if dropped:
        logger.info("Dropped %d blank or summary row(s)", dropped)
    return cleaned.loc[keep].reset_index(drop=True)

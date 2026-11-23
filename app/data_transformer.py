"""Cleaning, typing, mapping and schema alignment.

Order of operations, and why:

1. **Clean and type** the Amazon columns while they still carry Amazon's names.
   Typing is decided by column, not by "does this cell contain a ``$``" -- a
   campaign named ``SP | 50% off | $10 tier`` must survive as text.
2. **Parse dates** and work out which (country, date range) slices this report
   replaces.
3. **Rename** to the MySQL names via the heading map.
4. **Align** to the live table: drop frame columns the table does not have,
   send NULL for table columns the report does not fill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .exceptions import DataTransformationError
from .logging_config import get_logger
from .schema import (
    SCOPE_COLUMNS,
    SOURCE_COUNTRY_COLUMN,
    SOURCE_DATE_COLUMN,
    SOURCE_FLOAT_COLUMNS,
    SOURCE_INT_COLUMNS,
    SOURCE_TEXT_COLUMNS,
    REQUIRED_SOURCE_COLUMNS,
    TableSpec,
)

logger = get_logger("transform")

#: Everything that is not part of a signed decimal number.
_NON_NUMERIC = re.compile(r"[^0-9eE.+-]")
#: Accounting-style negatives: "(1,234.00)" means -1234.00.
_PARENTHESISED = re.compile(r"^\((.*)\)$")
#: Placeholders Amazon uses for "no value".
_NULL_TOKENS = frozenset({"", "-", "--", "—", "–", "n/a", "na", "null", "none", "nan"})
_MAX_TEXT_LENGTH = 512


@dataclass(frozen=True)
class ReplaceSlice:
    """One (country, date range) block of existing rows this report replaces."""

    country: str | None
    start: date
    end: date
    rows: int


@dataclass
class TransformResult:
    """Frame ready to write, plus what it replaces and what is worth logging."""

    frame: pd.DataFrame
    slices: list[ReplaceSlice]
    row_count: int
    first_date: date
    last_date: date
    scope: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    coercion_losses: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# value cleaning
# ---------------------------------------------------------------------------


def clean_numeric_value(value: object) -> float | None:
    """Parse one raw cell into a float.

    Handles ``$1,234.56``, ``12.5%``, ``(45.00)`` and the blank placeholders.
    Returns None when the cell holds no usable number.
    """
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (bool, np.bool_)):
        return float(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        return number if np.isfinite(number) else None

    text = str(value).strip()
    if text.lower() in _NULL_TOKENS:
        return None

    negative = False
    match = _PARENTHESISED.match(text)
    if match:
        negative = True
        text = match.group(1)

    cleaned = _NON_NUMERIC.sub("", text)
    if cleaned.startswith("-"):
        negative = True
    cleaned = cleaned.lstrip("+-")
    if cleaned.count(".") > 1:
        head, _, tail = cleaned.partition(".")
        cleaned = f"{head}.{tail.replace('.', '')}"
    if not cleaned or cleaned in {".", "e", "E"}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if not np.isfinite(number):
        return None
    return -number if negative else number


def clean_numeric_series(series: pd.Series) -> pd.Series:
    return series.map(clean_numeric_value).astype("Float64")


def _clean_text_series(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip()
    text = text.mask(text.str.lower().isin(_NULL_TOKENS), pd.NA)
    too_long = text.str.len() > _MAX_TEXT_LENGTH
    if bool(too_long.fillna(False).any()):
        logger.warning("Truncated %d over-long text value(s)", int(too_long.sum()))
        text = text.str.slice(0, _MAX_TEXT_LENGTH)
    return text


def parse_dates(series: pd.Series) -> pd.Series:
    """Parse Amazon's date cells into ``datetime.date``.

    XLSX cells arrive as real datetimes; CSV cells as text such as
    ``2026-10-24``, ``Oct 24, 2026`` or ``10/24/2026`` (US month-first).

    Raises:
        DataTransformationError: If any date cannot be parsed -- a row with no
            date cannot be placed, and guessing would put it on the wrong day.
    """
    def one(value: object) -> date | None:
        if value is None or value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, datetime):  # includes pd.Timestamp
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%b %d, %Y", "%m/%d/%Y", "%d-%b-%Y"):
            try:
                return pd.to_datetime(text, format=fmt).date()
            except (ValueError, TypeError):
                continue
        return None

    parsed = series.map(one)
    bad = parsed.isna() & series.notna()
    if bool(bad.any()):
        samples = ", ".join(repr(v) for v in series[bad].head(3).tolist())
        raise DataTransformationError(
            f"{int(bad.sum())} row(s) have a date that could not be parsed (e.g. {samples})"
        )
    if bool(parsed.isna().any()):
        raise DataTransformationError(f"{int(parsed.isna().sum())} row(s) have no date")
    return parsed


def coerce_types(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Cast each Amazon column to its storage type.

    Returns the frame plus a per-column count of values that held something in
    the source but could not be parsed as a number.
    """
    result = frame.copy()
    losses: dict[str, int] = {}

    result[SOURCE_DATE_COLUMN] = parse_dates(result[SOURCE_DATE_COLUMN])

    for column in SOURCE_TEXT_COLUMNS:
        if column in result.columns:
            result[column] = _clean_text_series(result[column])

    for column in SOURCE_INT_COLUMNS + SOURCE_FLOAT_COLUMNS:
        if column not in result.columns:
            continue
        original = result[column]
        as_text = original.astype("string").str.strip().str.lower()
        had_value = original.notna() & ~as_text.isin(_NULL_TOKENS)
        numeric = clean_numeric_series(original)
        lost = int((had_value & numeric.isna()).sum())
        if lost:
            losses[column] = lost
        if column in SOURCE_INT_COLUMNS:
            result[column] = numeric.round().astype("Int64")
        else:
            result[column] = numeric.round(2)

    if losses:
        logger.warning(
            "Values that could not be parsed as numbers: %s",
            ", ".join(f"{col}={count}" for col, count in sorted(losses.items())),
        )
    return result, losses


# ---------------------------------------------------------------------------
# mapping and alignment
# ---------------------------------------------------------------------------


def replace_slices(frame: pd.DataFrame) -> list[ReplaceSlice]:
    """The (country, first date, last date) blocks this report covers.

    Computed per country so a US report never deletes UK rows, and from the
    data rather than from configuration so the delete always matches exactly
    what is about to be inserted.
    """
    missing = "\x00no-country\x00"
    countries = frame[SOURCE_COUNTRY_COLUMN].astype("object").fillna(missing)
    slices = [
        ReplaceSlice(
            country=None if country == missing else str(country),
            start=min(dates),
            end=max(dates),
            rows=len(dates),
        )
        for country, dates in frame[SOURCE_DATE_COLUMN].groupby(countries)
    ]
    return sorted(slices, key=lambda s: (s.country or "", s.start))


def rename_columns(
    frame: pd.DataFrame, mapping: dict[str, str]
) -> tuple[pd.DataFrame, list[str]]:
    """Apply the heading map; return the renamed frame and unmapped columns."""
    applicable = {k: v for k, v in mapping.items() if k in frame.columns}
    unmapped = [str(c) for c in frame.columns if c not in applicable]
    renamed = frame.rename(columns=applicable)
    duplicates = renamed.columns[renamed.columns.duplicated()].tolist()
    if duplicates:
        raise DataTransformationError(
            f"Column mapping produced duplicate destination columns: {sorted(set(duplicates))}"
        )
    return renamed, unmapped


def align_to_table(frame: pd.DataFrame, spec: TableSpec) -> pd.DataFrame:
    """Reshape ``frame`` to the live table's writable columns, in table order.

    Columns the table does not have are dropped; columns the report does not
    fill are sent as NULL -- except ones the server defaults (``loaded_at``),
    which are left out so the server fills them.
    """
    if not spec.columns:
        raise DataTransformationError("Target table has no columns to align to")

    aligned = frame.copy()
    unknown = [c for c in aligned.columns if c not in spec.columns]
    if unknown:
        logger.warning(
            "%d report column(s) have no matching table column and will not be written: %s",
            len(unknown),
            ", ".join(map(str, unknown)),
        )

    wanted = [c for c in spec.columns if c in aligned.columns or c not in spec.defaulted_columns]
    absent = [c for c in wanted if c not in aligned.columns]
    if absent:
        logger.warning(
            "%d table column(s) are not filled by the report and will be written as NULL: %s",
            len(absent),
            ", ".join(absent),
        )
        for column in absent:
            aligned[column] = pd.NA
    return aligned.loc[:, wanted]


def conform_to_table_types(frame: pd.DataFrame, spec: TableSpec) -> pd.DataFrame:
    """Round values bound for INTEGER columns so strict-mode MySQL accepts them."""
    result = frame.copy()
    for column in spec.integer_columns:
        if column not in result.columns or str(result[column].dtype) == "Int64":
            continue
        result[column] = clean_numeric_series(result[column]).round().astype("Int64")
    return result


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def transform(
    frame: pd.DataFrame,
    mapping: dict[str, str],
    *,
    table: TableSpec,
    scope: dict[str, str] | None = None,
) -> TransformResult:
    """Turn a parsed report into the frame to write and the slices it replaces.

    Raises:
        DataTransformationError: If the report cannot be mapped onto the table.
    """
    if frame.empty:
        raise DataTransformationError(
            "Refusing to transform an empty report -- this would produce a delete-only write"
        )
    for required in (SOURCE_DATE_COLUMN, SOURCE_COUNTRY_COLUMN):
        if required not in mapping:
            raise DataTransformationError(
                f"The heading map has no destination for {required!r}; the loader needs it "
                "to know which existing rows to replace."
            )

    selected = frame.loc[:, list(REQUIRED_SOURCE_COLUMNS)]
    typed, losses = coerce_types(selected)
    slices = replace_slices(typed)

    renamed, unmapped = rename_columns(typed, mapping)
    if unmapped:
        logger.info("Columns without a mapping (not written): %s", ", ".join(unmapped))

    for column in SCOPE_COLUMNS:
        if scope and column in scope:
            renamed[column] = scope[column]

    out = conform_to_table_types(align_to_table(renamed, table), table)
    first, last = min(s.start for s in slices), max(s.end for s in slices)

    logger.info(
        "Transformed %d row(s) covering %s to %s | %s | scope=%s",
        len(out),
        first,
        last,
        "; ".join(f"{s.country}: {s.start}..{s.end} ({s.rows} rows)" for s in slices),
        scope or "(none)",
    )
    return TransformResult(
        frame=out,
        slices=slices,
        row_count=len(out),
        first_date=first,
        last_date=last,
        scope=dict(scope or {}),
        unmapped_columns=unmapped,
        coercion_losses=losses,
    )


def to_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Convert a frame to driver-friendly records (real NULLs, Python scalars)."""
    records: list[dict[str, object]] = []
    for row in frame.to_dict(orient="records"):
        record: dict[str, object] = {}
        for key, value in row.items():
            if value is None or value is pd.NA or value is pd.NaT:
                record[key] = None
            elif isinstance(value, float) and np.isnan(value):
                record[key] = None
            elif isinstance(value, np.integer):
                record[key] = int(value)
            elif isinstance(value, np.floating):
                record[key] = None if np.isnan(value) else float(value)
            elif isinstance(value, np.bool_):
                record[key] = bool(value)
            elif isinstance(value, pd.Timestamp):
                record[key] = value.date()
            else:
                record[key] = value
        records.append(record)
    return records


def describe_alignment(
    frame_columns: Iterable[str], table_columns: Sequence[str]
) -> dict[str, list[str]]:
    """Diff a mapped frame against a live table -- used by the dry-run report."""
    frame_set, table_set = set(frame_columns), set(table_columns)
    return {
        "unwritable": sorted(frame_set - table_set),
        "null_filled": sorted(table_set - frame_set),
    }

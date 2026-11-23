"""Report and database schema definitions.

Cleaning is keyed on **Amazon's** column names (stable and known); the MySQL
names come from ``config/Purchased_Product_Heading.csv`` and are ours to change.
Values are cleaned and typed first, then renamed.

The live MySQL table is authoritative for the column set: it is reflected at
runtime and the frame is aligned to what is really there.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Column,
    Date,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    TIMESTAMP,
    func,
    Index,
)

#: Amazon header holding each row's date.  Every row carries its own date, so
#: one download can cover many days.
SOURCE_DATE_COLUMN = "Date"
#: Amazon header identifying the marketplace.  Rows are replaced per country so
#: loading a US report can never delete another marketplace's rows.
SOURCE_COUNTRY_COLUMN = "Country"

#: Free text -- never numeric-cleaned, however much punctuation it contains.
SOURCE_TEXT_COLUMNS: tuple[str, ...] = (
    "Portfolio name",
    "Campaign Name",
    "Country",
    "Currency",
    "Ad Group Name",
    "Retailer",
    "Advertised SKU",
    "Advertised ASIN",
    "Targeting",
    "Match Type",
    "Purchased ASIN",
)

#: Whole-number metrics, by Amazon column name.
SOURCE_INT_COLUMNS: tuple[str, ...] = (
    "7 Day Other SKU Units (#)",
    "7 Day Other SKU Orders (#)",
)

#: Decimal metrics, by Amazon column name.
SOURCE_FLOAT_COLUMNS: tuple[str, ...] = ("7 Day Other SKU Sales",)

#: Every column the export is expected to contain, in export order.
REQUIRED_SOURCE_COLUMNS: tuple[str, ...] = (
    (SOURCE_DATE_COLUMN,) + SOURCE_TEXT_COLUMNS + SOURCE_INT_COLUMNS + SOURCE_FLOAT_COLUMNS
)

#: A file missing any of these cannot be loaded safely: the date and country
#: decide which existing rows are replaced, and the ASINs are the report.
IDENTITY_COLUMNS: tuple[str, ...] = (
    SOURCE_DATE_COLUMN,
    SOURCE_COUNTRY_COLUMN,
    "Advertised ASIN",
    "Purchased ASIN",
)

#: Default table name; overridable via DB_TABLE.
DEFAULT_TABLE = "sponsored_products_purchased_product_report"

#: Destination columns the loader keys on.  Configurable because they come
#: from the heading map; these are the shipped names.
DEFAULT_REPORT_DATE_COLUMN = "report_date"
DEFAULT_COUNTRY_COLUMN = "country"

#: Optional columns that identify *whose* rows these are when several brands or
#: accounts share one table.  Only used if the live table has them.
SCOPE_COLUMNS: tuple[str, ...] = ("brand_code",)

#: Text column widths for a fresh install.
_TEXT_LENGTHS: dict[str, int] = {
    "Country": 64,
    "Currency": 8,
    "Retailer": 64,
    "Advertised SKU": 128,
    "Advertised ASIN": 20,
    "Purchased ASIN": 20,
    "Targeting": 512,
    "Match Type": 32,
}


@dataclass(frozen=True)
class TableSpec:
    """What the loader needs to know about a live target table."""

    name: str
    #: Writable columns, in table order.  Auto-increment keys are excluded.
    columns: tuple[str, ...]
    #: Columns declared INTEGER/BIGINT: values are rounded before insert, so a
    #: server in STRICT_TRANS_TABLES mode does not reject the row.
    integer_columns: frozenset[str] = frozenset()
    #: Columns the server fills itself when omitted (e.g. ``loaded_at``).
    #: Left out of the INSERT unless the report supplies them -- sending NULL
    #: to a ``TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`` fails in strict mode.
    defaulted_columns: frozenset[str] = frozenset()

    @classmethod
    def from_table(cls, table: Table) -> TableSpec:
        columns = tuple(c.name for c in table.columns if not _is_generated(c))
        integers = frozenset(
            c.name
            for c in table.columns
            if c.name in columns and isinstance(c.type, Integer)
        )
        defaulted = frozenset(
            c.name for c in table.columns if c.name in columns and c.server_default is not None
        )
        return cls(
            name=table.name,
            columns=columns,
            integer_columns=integers,
            defaulted_columns=defaulted,
        )


def _is_generated(column: Column) -> bool:
    """Whether the database fills this column itself (an AUTO_INCREMENT key)."""
    if not column.primary_key or not isinstance(column.type, Integer):
        return False
    table = column.table
    if table is not None and len(table.primary_key.columns) != 1:
        return False
    return column.autoincrement in (True, "auto")


def build_bootstrap_table(
    mapping: dict[str, str],
    *,
    table_name: str = DEFAULT_TABLE,
    metadata: MetaData | None = None,
) -> Table:
    """DDL for a *fresh* install, derived from the heading map.

    Only used when ``CREATE_TABLES=true`` or by the tests.  An existing table is
    always reflected instead -- it is never altered.
    """
    metadata = metadata or MetaData()
    date_col = mapping.get(SOURCE_DATE_COLUMN, DEFAULT_REPORT_DATE_COLUMN)
    country_col = mapping.get(SOURCE_COUNTRY_COLUMN, DEFAULT_COUNTRY_COLUMN)

    columns: list[Column] = [Column("id", Integer, primary_key=True, autoincrement=True)]
    seen: set[str] = {"id"}
    for source in REQUIRED_SOURCE_COLUMNS:
        destination = mapping.get(source)
        if not destination or destination in seen:
            continue
        seen.add(destination)
        if source == SOURCE_DATE_COLUMN:
            columns.append(Column(destination, Date, nullable=False))
        elif source in SOURCE_TEXT_COLUMNS:
            columns.append(Column(destination, String(_TEXT_LENGTHS.get(source, 255)), nullable=True))
        elif source in SOURCE_INT_COLUMNS:
            columns.append(Column(destination, Integer, nullable=True))
        else:
            columns.append(Column(destination, Numeric(14, 2), nullable=True))
    columns.append(
        Column("loaded_at", TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    )

    table = Table(table_name, metadata, *columns, mysql_charset="utf8mb4")
    Index("idx_country_date", table.c[country_col], table.c[date_col])
    Index("idx_report_date", table.c[date_col])
    for asin in ("Advertised ASIN", "Purchased ASIN"):
        name = mapping.get(asin)
        if name:
            Index(f"idx_{name}", table.c[name])
    return table

"""MySQL persistence.

The target table is **reflected** and the pipeline writes what is really there.
It never issues DDL against an existing table; ``CREATE_TABLES=true`` only
creates the table when it is entirely absent.

Every load is one explicit transaction: for each (country, date range) the
report covers, DELETE the existing rows, then INSERT the report's rows, and
COMMIT once.  The table is never observed with rows deleted but not replaced,
a failed insert rolls the deletes back with it, and a delete is only ever
issued alongside rows to insert -- an empty report cannot erase data.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from sqlalchemy import (
    MetaData,
    Table,
    and_,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import DatabaseConfig, Settings
from .data_transformer import ReplaceSlice
from .exceptions import DatabaseError
from .logging_config import get_logger
from .schema import (
    SCOPE_COLUMNS,
    SOURCE_COUNTRY_COLUMN,
    SOURCE_DATE_COLUMN,
    TableSpec,
    build_bootstrap_table,
)

logger = get_logger("database")

#: Rows per INSERT batch: fast, while staying under a default max_allowed_packet.
INSERT_CHUNK_SIZE = 500

#: MySQL errors a second attempt can plausibly get past.
TRANSIENT_MYSQL_ERRNOS = frozenset(
    {
        1205,  # lock wait timeout exceeded
        1213,  # deadlock found
        2003,  # can't connect to server
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection (I/O error)
    }
)


def _database_error(message: str, exc: SQLAlchemyError) -> DatabaseError:
    """Wrap a driver failure, marking it transient when a retry could succeed."""
    error = DatabaseError(message)
    errno = getattr(getattr(exc, "orig", None), "errno", None)
    error.transient = bool(
        getattr(exc, "connection_invalidated", False) or errno in TRANSIENT_MYSQL_ERRNOS
    )
    return error


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one load."""

    table: str
    deleted: int
    inserted: int


def create_db_engine(config: DatabaseConfig, *, echo: bool = False) -> Engine:
    """Build a pooled engine.  The URL is never logged with its password."""
    try:
        engine = create_engine(
            config.url,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=config.pool_recycle_seconds,
            connect_args=config.connect_args(),
        )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"Could not create database engine for {config.safe_url}: {exc}") from exc
    logger.debug("Engine created for %s", config.safe_url)
    return engine


class ReportRepository:
    """All database access for the report table."""

    def __init__(self, engine: Engine, *, table: str, mapping: dict[str, str]) -> None:
        self.engine = engine
        self.table_name = table
        self.mapping = mapping
        self.date_column = mapping[SOURCE_DATE_COLUMN]
        self.country_column = mapping[SOURCE_COUNTRY_COLUMN]
        self._metadata = MetaData()
        self._table: Table | None = None

    # -- connectivity / setup ----------------------------------------------

    def check_connection(self) -> None:
        """Fail fast if MySQL is unreachable."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise _database_error(f"Database connection failed: {exc}", exc) from exc
        logger.info("Database connection verified")

    def create_table_if_missing(self) -> bool:
        """Create the table from the heading map if it does not exist.

        An existing table is left exactly as it is.  Returns whether it created.
        """
        try:
            exists = inspect(self.engine).has_table(self.table_name)
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not list tables: {exc}") from exc
        if exists:
            return False
        metadata = MetaData()
        table = build_bootstrap_table(self.mapping, table_name=self.table_name, metadata=metadata)
        try:
            metadata.create_all(self.engine, tables=[table])
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not create table {self.table_name}: {exc}") from exc
        logger.info("Created missing table %s", self.table_name)
        return True

    def reflect(self) -> None:
        """Load the live table definition.

        Raises:
            DatabaseError: If the table is missing, or lacks the date/country
                columns the load keys on.
        """
        try:
            table = Table(self.table_name, self._metadata, autoload_with=self.engine)
        except SQLAlchemyError as exc:
            raise _database_error(
                f"Could not reflect table {self.table_name!r}: {exc}. Create it with the DDL "
                "in docs/setup.md, or set CREATE_TABLES=true.",
                exc,
            ) from exc
        for needed in (self.date_column, self.country_column):
            if needed not in table.columns:
                raise DatabaseError(
                    f"Table {self.table_name!r} has no column {needed!r}, which the load keys "
                    "on. Fix the heading map or the table."
                )
        self._table = table
        logger.info("Reflected %s with %d column(s)", self.table_name, len(table.columns))

    @property
    def table(self) -> Table:
        if self._table is None:
            self.reflect()
        assert self._table is not None
        return self._table

    def spec(self) -> TableSpec:
        return TableSpec.from_table(self.table)

    def resolve_scope(self, configured: dict[str, str]) -> dict[str, str]:
        """Check the configured scope against the live table and return it.

        Raises:
            DatabaseError: If the table has a scope column with no configured
                value -- the delete would then match every brand's rows.
        """
        present = [c for c in SCOPE_COLUMNS if c in self.table.columns]
        missing = [c for c in present if not configured.get(c)]
        if missing:
            raise DatabaseError(
                f"Table {self.table_name!r} has scope column(s) {', '.join(missing)} but no "
                f"value is configured. Set {', '.join(c.upper() for c in missing)}. Refusing "
                "to run: the delete would otherwise remove every brand's rows."
            )
        unused = sorted(set(configured) - set(present))
        if unused:
            logger.warning(
                "Configured scope %s has no matching column in %s and is ignored",
                ", ".join(unused),
                self.table_name,
            )
        return {c: configured[c] for c in present}

    def validate_schema(self) -> list[str]:
        """Log mapped columns that the live table does not have."""
        live = set(self.table.columns.keys())
        unwritable = sorted(set(self.mapping.values()) - live)
        if unwritable:
            logger.warning(
                "%d mapped column(s) do not exist in %s and will not be written: %s",
                len(unwritable),
                self.table_name,
                ", ".join(unwritable),
            )
        logger.info("Schema validated against the live table")
        return unwritable

    # -- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Explicit BEGIN / COMMIT, with ROLLBACK on any exception."""
        connection = self.engine.connect()
        try:
            transaction = connection.begin()
            try:
                yield connection
            except Exception as exc:
                try:
                    transaction.rollback()
                except SQLAlchemyError as rollback_exc:
                    logger.warning("ROLLBACK failed: %s", rollback_exc)
                logger.error("ROLLBACK after error: %s: %s", type(exc).__name__, exc)
                raise
            transaction.commit()
        finally:
            connection.close()

    # -- writes ------------------------------------------------------------

    def _slice_criteria(self, part: ReplaceSlice, scope: dict[str, str]):
        table = self.table
        country = table.c[self.country_column]
        criteria = [
            country.is_(None) if part.country is None else country == part.country,
            table.c[self.date_column].between(part.start, part.end),
        ]
        criteria.extend(table.c[name] == value for name, value in scope.items())
        return and_(*criteria)

    def replace_slices(
        self,
        slices: Sequence[ReplaceSlice],
        records: Sequence[dict[str, object]],
        scope: dict[str, str] | None = None,
    ) -> WriteResult:
        """Replace every (country, date range) slice with ``records``, atomically.

        Raises:
            ValueError: If ``records`` or ``slices`` is empty -- deleting without
                replacing is never a valid load.
            DatabaseError: On any database failure; the transaction is rolled
                back first, so the existing data survives intact.
        """
        if not records or not slices:
            raise ValueError("replace_slices called with nothing to insert; refusing to delete")
        scope = scope or {}

        try:
            with self.transaction() as connection:
                deleted = 0
                for part in slices:
                    count = connection.execute(
                        delete(self.table).where(self._slice_criteria(part, scope))
                    ).rowcount
                    deleted += max(count, 0)
                    logger.info(
                        "Deleted %d existing row(s) for %s %s..%s%s",
                        count,
                        part.country,
                        part.start,
                        part.end,
                        f" ({', '.join(f'{k}={v}' for k, v in scope.items())})" if scope else "",
                    )
                inserted = _chunked_insert(connection, self.table, records)
        except SQLAlchemyError as exc:
            raise _database_error(f"Load failed and was rolled back: {exc}", exc) from exc

        logger.info(
            "Load committed: %d deleted, %d inserted into %s", deleted, inserted, self.table_name
        )
        return WriteResult(table=self.table_name, deleted=deleted, inserted=inserted)

    def count_between(
        self, start: date, end: date, *, country: str | None = None, scope: dict[str, str] | None = None
    ) -> int:
        """Row count for a date range -- used by verification and tests."""
        table = self.table
        criteria = [table.c[self.date_column].between(start, end)]
        if country is not None:
            criteria.append(table.c[self.country_column] == country)
        criteria.extend(table.c[k] == v for k, v in (scope or {}).items())
        try:
            with self.engine.connect() as connection:
                return int(
                    connection.execute(
                        select(func.count()).select_from(table).where(and_(*criteria))
                    ).scalar_one()
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"Could not count rows in {self.table_name}: {exc}") from exc

    def dispose(self) -> None:
        self.engine.dispose()


def _chunked_insert(
    connection: Connection, table: Table, records: Sequence[dict[str, object]]
) -> int:
    total = 0
    for start in range(0, len(records), INSERT_CHUNK_SIZE):
        chunk = list(records[start : start + INSERT_CHUNK_SIZE])
        connection.execute(insert(table), chunk)
        total += len(chunk)
    return total


def build_repository(
    settings: Settings, mapping: dict[str, str], *, echo: bool = False
) -> ReportRepository:
    """Create a repository, verify connectivity, and reflect the live table."""
    repository = ReportRepository(
        create_db_engine(settings.database, echo=echo), table=settings.table, mapping=mapping
    )
    try:
        repository.check_connection()
        if settings.create_tables:
            repository.create_table_if_missing()
        repository.reflect()
        repository.validate_schema()
    except Exception:
        repository.dispose()
        raise
    return repository

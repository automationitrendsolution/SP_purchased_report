"""Pipeline orchestration and execution modes.

    Create report (/reports/new) -> Run report -> reload Reports until
    ready -> download (.xlsx)
        -> pandas -> MySQL

Each stage raises its own exception type (see :mod:`app.exceptions`); this
module decides what that means for the run, what gets logged, and what the
process exits with.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .amazon_ads import ReportsPage
from .auth import sign_in, sign_in_interactive
from .browser import BrowserSession
from .config import Settings
from .data_transformer import TransformResult, to_records, transform
from .database import ReportRepository, WriteResult, build_repository
from .downloader import DownloadResult, download_report
from .exceptions import AuthenticationError, DatabaseError, EmptyReportError, ReportFlowError
from .logging_config import get_logger
from .maintenance import cleanup_old_files
from .report_processor import ReadResult, load_heading_map, read_report
from .retry import retry_call
from .runlock import run_lock
from .schema import TableSpec, build_bootstrap_table
from .timeutils import now_in, now_utc, today_in, utc_stamp

logger = get_logger("pipeline")

T = TypeVar("T")


class RunMode(str, Enum):
    """What a single invocation is supposed to do."""

    FULL = "full"
    DOWNLOAD_ONLY = "download-only"
    PROCESS_FILE = "process-file"
    LOGIN = "login"


@dataclass
class RunOptions:
    """Per-invocation switches, all settable from the CLI."""

    mode: RunMode = RunMode.FULL
    dry_run: bool = False
    headed: bool | None = None
    process_file: Path | None = None
    use_lock: bool = True
    echo_sql: bool = False

    @property
    def writes_database(self) -> bool:
        return not self.dry_run and self.mode in {RunMode.FULL, RunMode.PROCESS_FILE}

    @property
    def label(self) -> str:
        return f"{self.mode.value} (dry-run)" if self.dry_run else self.mode.value


@dataclass
class RunSummary:
    """Everything the run is expected to report on."""

    mode: str
    started_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    report_name: str | None = None
    download_path: Path | None = None
    download_bytes: int = 0
    first_date: date | None = None
    last_date: date | None = None
    row_count: int = 0
    scope: dict[str, str] = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)
    write_result: WriteResult | None = None
    error: str | None = None
    succeeded: bool = False

    @property
    def duration_seconds(self) -> float:
        return ((self.finished_at or now_utc()) - self.started_at).total_seconds()

    def log(self) -> None:
        logger.info("=" * 72)
        logger.info("Run summary (%s)", self.mode)
        logger.info("  started (UTC)      : %s", self.started_at)
        logger.info("  duration           : %.1fs", self.duration_seconds)
        if self.report_name:
            logger.info("  Amazon report      : %s", self.report_name)
        logger.info("  report file        : %s", self.download_path)
        logger.info(
            "  file size          : %s",
            f"{self.download_bytes / 1024:.1f} KB" if self.download_bytes else "n/a",
        )
        logger.info("  dates covered      : %s .. %s", self.first_date, self.last_date)
        logger.info("  rows processed     : %d", self.row_count)
        logger.info(
            "  row scope          : %s",
            ", ".join(f"{k}={v}" for k, v in self.scope.items()) if self.scope else "(none)",
        )
        logger.info(
            "  missing columns    : %s",
            ", ".join(self.missing_columns) if self.missing_columns else "none",
        )
        if self.write_result:
            logger.info(
                "  %s: %d deleted, %d inserted",
                self.write_result.table,
                self.write_result.deleted,
                self.write_result.inserted,
            )
        logger.info("  status             : %s", "SUCCESS" if self.succeeded else "FAILED")
        if self.error:
            logger.info("  error              : %s", self.error)
        logger.info("=" * 72)


class ReportPipeline:
    """Runs one execution end to end."""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        repository_factory: Callable[[], ReportRepository] | None = None,
    ) -> None:
        """
        Args:
            repository_factory: Overrides how the repository is built.  Used by
                tests to substitute a local database; production leaves it None.
        """
        self.settings = settings
        self.options = options
        self.summary = RunSummary(mode=options.label)
        self._mapping: dict[str, str] | None = None
        self._scope: dict[str, str] = {}
        #: Name of the report this run created in Amazon, once Run was clicked.
        self._created_report: str | None = None
        self._owns_repository = repository_factory is None
        self._repository_factory = repository_factory or (
            lambda: build_repository(settings, self.mapping, echo=options.echo_sql)
        )

    @property
    def mapping(self) -> dict[str, str]:
        if self._mapping is None:
            self._mapping = load_heading_map(self.settings.heading_map_file)
            logger.info(
                "Loaded %d column mapping(s) from %s",
                len(self._mapping),
                self.settings.heading_map_file,
            )
        return self._mapping

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        """Execute the configured mode, returning the summary either way."""
        self.settings.ensure_directories()
        self._log_start()
        try:
            with run_lock(self.settings.lock_file, enabled=self.options.use_lock):
                if self.options.mode is RunMode.LOGIN:
                    self._run_login()
                elif self.options.mode is RunMode.DOWNLOAD_ONLY:
                    self._run_download_only()
                elif self.options.mode is RunMode.PROCESS_FILE:
                    self._run_process_file()
                else:
                    self._run_full()
            self.summary.succeeded = True
        except ReportFlowError as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.error("Run failed: %s", self.summary.error)
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced after summary
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Unexpected failure")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        logger.info("=" * 72)
        logger.info("Purchased product report pipeline starting | mode=%s", self.options.label)
        logger.info("  UTC            : %s", now_utc().isoformat())
        logger.info(
            "  %-14s : %s",
            str(self.settings.report_timezone),
            now_in(self.settings.report_timezone).isoformat(),
        )
        for key, value in self.settings.describe().items():
            logger.info("  %-14s : %s", key, value)
        logger.info("=" * 72)

    # -- modes -------------------------------------------------------------

    def _run_full(self) -> None:
        repository = self._open_repository()
        try:
            download = self._download_with_retry()
            try:
                self._load_file(download.path, repository)
            except Exception:
                self._archive(download.path, success=False)
                raise
            if self.options.writes_database and not self.settings.keep_processed_files:
                self._discard(download.path)
            else:
                self._archive(download.path, success=True)
        finally:
            # Housekeeping also runs when the run failed -- otherwise a stretch
            # of failing runs prunes nothing -- but never masks the real error.
            try:
                cleanup_old_files(self.settings)
            except Exception:
                logger.warning("Housekeeping failed", exc_info=True)
            if repository is not None and self._owns_repository:
                repository.dispose()

    def _run_download_only(self) -> None:
        download = self._download_with_retry()
        logger.info(
            "Download-only complete: %s (%.1f KB). No processing performed.",
            download.path,
            download.size_kb,
        )

    def _run_process_file(self) -> None:
        source = self.options.process_file
        assert source is not None  # guaranteed by the CLI
        source = Path(source).expanduser().resolve()
        logger.info("Processing existing file: %s", source)
        self.summary.download_path = source
        if source.is_file():
            self.summary.download_bytes = source.stat().st_size

        repository = self._open_repository()
        try:
            self._load_file(source, repository)
        finally:
            if repository is not None and self._owns_repository:
                repository.dispose()

    def _run_login(self) -> None:
        """Establish the Amazon session with a person at the keyboard.

        * **Headed** -- a real browser, when ``DISPLAY`` is set (or on Windows /
          macOS), or when ``--headed`` forces it.  The person finishes whatever
          Amazon asks on screen, CAPTCHAs included.
        * **Terminal** -- headless Chromium on an SSH-only server.  Amazon's
          two-step code is typed at the prompt; a CAPTCHA stops the run with a
          screenshot to look at.

        Either way Chromium persists the session in ``SESSION_DIR``.
        """
        if not self._can_show_a_window():
            self._run_login_terminal()
            return
        logger.info("Opening a headed browser for Amazon sign-in")
        with BrowserSession(self.settings, headless=False) as session:
            reports = ReportsPage(session, self.settings)
            reports.goto(self.settings.new_report_url_with_entity, "Create report")

            if self.settings.has_credentials and reports.is_showing_sign_in():
                print("\nFilling in the configured credentials...")
                try:
                    sign_in(session, self.settings)
                except AuthenticationError as exc:
                    logger.info("Automatic sign-in stopped: %s", exc)
                    print(f"\nAmazon needs you to finish this by hand:\n  {exc}\n")

            print("\n" + "=" * 72)
            print("Finish anything Amazon still asks for (OTP / MFA / CAPTCHA) in the")
            print("browser window, until the Create report page is visible. Then return here")
            print("and press Enter.")
            print("=" * 72 + "\n")
            input("Press Enter once you can see the Create report page... ")
            self._verify_session(reports)

    def _can_show_a_window(self) -> bool:
        if self.options.headed:
            return True
        if sys.platform in ("win32", "darwin"):
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def _run_login_terminal(self) -> None:
        logger.info("No display detected; signing in with the terminal prompt")
        print("\n" + "=" * 72)
        print("Amazon sign-in (no display -- codes are typed here)")
        print("=" * 72)
        with BrowserSession(self.settings, headless=True) as session:
            reports = ReportsPage(session, self.settings)
            reports.goto(self.settings.new_report_url_with_entity, "Create report")
            if reports.is_showing_sign_in():
                sign_in_interactive(session, self.settings)
            else:
                print("\nAlready signed in -- the saved session is still good.")
            reports.goto(self.settings.new_report_url_with_entity, "Create report")
            self._verify_session(reports)

    def _verify_session(self, reports: ReportsPage) -> None:
        print("\nChecking the session (this takes a few seconds)...")
        reports.assert_authenticated()
        print("  ✓ signed in -- no sign-in page or challenge on screen")
        print("  · looking for the Create report page...")
        reports.wait_for("report_builder_marker", "Create report")
        print("  ✓ Create report page found")
        print("  · looking for the Reports list...")
        reports.open_list()
        print("  ✓ Reports list found")
        logger.info("Session established and saved to %s", self.settings.session_dir)
        print(f"\nSession stored in {self.settings.session_dir}")
        print("Scheduled runs can use it now. Verify with:")
        print("    uv run python run.py --dry-run\n")

    # -- stages ------------------------------------------------------------

    def _open_repository(self) -> ReportRepository | None:
        """Connect to MySQL, or return None when a dry run cannot reach it."""
        if self.options.mode is RunMode.DOWNLOAD_ONLY:
            return None
        try:
            repository = self._retry("Database connection", self._repository_factory)
        except DatabaseError:
            if self.options.writes_database:
                raise
            logger.warning("DRY RUN: MySQL is unreachable; continuing without database validation")
            return None
        # Outside the except on purpose: an unusable scope is a configuration
        # error that a dry run exists to catch.
        self._scope = repository.resolve_scope(self.settings.scope)
        return repository

    def _download_with_retry(self) -> DownloadResult:
        """Create the report, wait for it, download it -- retrying in a fresh browser.

        The report is created at most once per run.  Once Run report has been
        clicked it exists in Amazon, so a retry after that point only goes back
        to waiting for the same report instead of creating a duplicate.
        """

        def attempt() -> DownloadResult:
            with BrowserSession(self.settings, headless=self._headless()) as session:
                reports = ReportsPage(session, self.settings)
                if self._created_report is None:
                    name = f"{self.settings.report_name_prefix} {utc_stamp()}"
                    reports.open_builder()
                    reports.create_report(name)
                    self._created_report = name
                    self.summary.report_name = name
                else:
                    logger.info("Reusing report %r created by an earlier attempt", self._created_report)
                return download_report(session, reports, self.settings, self._created_report)

        result = self._retry("Report download", attempt)
        self.summary.download_path = result.path
        self.summary.download_bytes = result.size_bytes
        return result

    def _headless(self) -> bool:
        return self.settings.headless if self.options.headed is None else not self.options.headed

    def _load_file(self, path: Path, repository: ReportRepository | None) -> None:
        read_result = read_report(path)
        transformed = self._transform(read_result, repository)
        self._check_freshness(transformed.last_date)
        if repository is not None and self.options.writes_database:
            self._write(repository, transformed)
        else:
            logger.info(
                "DRY RUN: %d row(s) prepared (%s..%s); nothing written to MySQL",
                transformed.row_count,
                transformed.first_date,
                transformed.last_date,
            )

    def _transform(
        self, read_result: ReadResult, repository: ReportRepository | None
    ) -> TransformResult:
        """Map and type the report, aligned to the live table when available."""
        self.summary.missing_columns = list(read_result.missing_columns)
        if repository is not None:
            spec = repository.spec()
        else:
            spec = TableSpec.from_table(
                build_bootstrap_table(self.mapping, table_name=self.settings.table)
            )
            logger.warning(
                "No database connection: aligning against the column set derived from %s",
                self.settings.heading_map_file.name,
            )
        transformed = transform(
            read_result.frame,
            self.mapping,
            table=spec,
            scope=self._scope or self.settings.scope,
        )
        self.summary.row_count = transformed.row_count
        self.summary.first_date = transformed.first_date
        self.summary.last_date = transformed.last_date
        self.summary.scope = transformed.scope
        return transformed

    def _check_freshness(self, last_date: date) -> None:
        """Warn when the newest data is old: the schedule has probably stopped.

        A warning, not a failure -- reloading an older report is harmless (it
        replaces the same rows with the same values), but nobody should have
        to discover a dead schedule from a dashboard a week later.
        """
        today = today_in(self.settings.report_timezone)
        if today - last_date > timedelta(days=self.settings.report_max_age_days):
            logger.warning(
                "The newest date in this report is %s, %d day(s) before today (%s, %s). "
                "Check AMAZON_REPORT_PERIOD / the console's default Report period.",
                last_date,
                (today - last_date).days,
                today,
                self.settings.report_timezone,
            )

    def _write(self, repository: ReportRepository, transformed: TransformResult) -> None:
        if transformed.row_count == 0:
            raise EmptyReportError(
                "Transformed report has zero rows; skipping all writes so existing data is preserved"
            )
        records = to_records(transformed.frame)
        self.summary.write_result = self._retry(
            "Database load",
            lambda: repository.replace_slices(transformed.slices, records, scope=self._scope),
        )

    def _retry(self, description: str, operation: Callable[[], T]) -> T:
        """Retry the failures a second attempt can fix.

        Safe for every step it wraps: connecting and downloading are read-only,
        and the load is one DELETE+INSERT transaction, so re-running it
        converges on the same rows.
        """
        return retry_call(
            operation,
            description=description,
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )

    def _discard(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            logger.info("Deleted report %s now that it is loaded", path)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)

    def _archive(self, path: Path, *, success: bool) -> None:
        """Move a download into ``processed/`` or ``failed/``."""
        if not path.exists():
            return
        destination_dir = self.settings.processed_dir if success else self.settings.failed_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / path.name
        try:
            shutil.move(str(path), str(destination))
            logger.info("Archived report to %s", destination)
        except OSError as exc:
            logger.warning("Could not archive %s to %s: %s", path, destination, exc)


def handle_failure(pipeline: ReportPipeline, error: BaseException) -> None:
    """Emit the operator-facing guidance for a failed run."""
    if isinstance(error, AuthenticationError):
        logger.error(
            "Authentication is required and cannot be automated. Run "
            "'uv run python run.py --login' to refresh the session."
        )
    if isinstance(error, EmptyReportError):
        logger.error("The report contained no rows. Nothing was deleted or inserted.")

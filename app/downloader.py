"""Report download: trigger, capture, and validate.

The download is captured through Playwright's ``expect_download`` event rather
than by polling a directory, so the file is only accepted once Chromium reports
the transfer finished -- no guessing, no partial ``.crdownload`` reads.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Download, TimeoutError as PlaywrightTimeoutError

from .amazon_ads import ReportsPage
from .browser import BrowserSession
from .config import Settings
from .exceptions import DownloadError, ReportValidationError
from .logging_config import get_logger
from .timeutils import utc_stamp

logger = get_logger("downloader")

#: Anything smaller than this is a truncated or error response, not a report.
MIN_FILE_BYTES = 32
#: Leading bytes that mean Amazon served a web page instead of a report.
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml")
#: XLSX and ZIP are both ZIP containers.
_ZIP_MAGIC = b"PK\x03\x04"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ACCEPTED_SUFFIXES = (".xlsx", ".csv", ".zip")


@dataclass(frozen=True)
class DownloadResult:
    """A validated file on local disk."""

    path: Path
    size_bytes: int
    suggested_filename: str

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024


def download_report(
    session: BrowserSession, reports: ReportsPage, settings: Settings, report_name: str
) -> DownloadResult:
    """Wait for ``report_name`` to be ready, download it, return the validated file.

    Raises:
        DownloadError: If the report is not ready in time or no download arrived.
        ReportNotFoundError: If the report never appears in the list.
        ReportValidationError: If the downloaded file is not a usable report.
    """
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    control = reports.wait_until_downloadable(report_name)
    download = _click_and_wait(session, control, settings)

    failure = download.failure()
    if failure:
        session.capture_failure("download-failed")
        raise DownloadError(f"Download failed: {failure}")

    suggested = download.suggested_filename or "purchased_product.xlsx"
    target = settings.download_dir / target_filename(suggested)
    try:
        download.save_as(str(target))
    except Exception as exc:
        session.capture_failure("download-save-failed")
        raise DownloadError(f"Could not save download to {target}: {exc}") from exc

    logger.info("Download captured: %s (suggested name %s)", target, suggested)
    size = validate_download(target)
    return DownloadResult(path=target, size_bytes=size, suggested_filename=suggested)


def _click_and_wait(session: BrowserSession, control, settings: Settings) -> Download:
    try:
        with session.page.expect_download(timeout=settings.download_timeout_ms) as info:
            control.click()
        return info.value
    except PlaywrightTimeoutError as exc:
        session.capture_failure("download-timeout")
        raise DownloadError(
            f"Clicked Download, but no file arrived within {settings.download_timeout_ms} ms. "
            "Raise DOWNLOAD_TIMEOUT_MS if this persists."
        ) from exc
    except Exception as exc:
        session.capture_failure("download-error")
        raise DownloadError(f"Clicking Download failed: {exc}") from exc


def target_filename(suggested: str) -> str:
    """Timestamped, filesystem-safe name so two runs never collide."""
    safe = _SAFE_NAME.sub("_", Path(suggested).name).strip("_") or "purchased_product.xlsx"
    if not safe.lower().endswith(_ACCEPTED_SUFFIXES):
        safe = f"{safe}.xlsx"
    return f"{utc_stamp()}_{safe}"


def validate_download(path: Path) -> int:
    """Check that ``path`` is a plausible report file and return its size.

    Raises:
        ReportValidationError: If the file is missing, empty, an HTML page, a
            corrupt workbook, or text with no delimited header.
    """
    if not path.is_file():
        raise ReportValidationError(f"Downloaded file does not exist: {path}")

    size = path.stat().st_size
    if size < MIN_FILE_BYTES:
        raise ReportValidationError(f"Downloaded file {path} is {size} bytes -- too small")

    with path.open("rb") as handle:
        head = handle.read(4096)

    if head.lstrip().lower().startswith(_HTML_PREFIXES):
        raise ReportValidationError(
            f"Downloaded file {path} looks like an HTML page, not a report. "
            "This usually means the session expired mid-download."
        )

    if head.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
        except zipfile.BadZipFile as exc:
            raise ReportValidationError(f"{path} is a corrupt XLSX/ZIP: {exc}") from exc
        if bad is not None:
            raise ReportValidationError(f"{path} is a corrupt XLSX/ZIP (member {bad})")
    else:
        text = head.decode("utf-8-sig", errors="replace")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if "," not in first_line and "\t" not in first_line:
            raise ReportValidationError(
                f"{path} is neither a workbook nor delimited text: {first_line[:120]!r}"
            )

    logger.info("Download validated: %s (%.1f KB)", path, size / 1024)
    return size

"""Small units: config, selectors, download validation, retry, lock, housekeeping,
log redaction and the CLI's early exits."""

from __future__ import annotations

import logging
import os
import time
import zipfile
from pathlib import Path

import pytest

from app.config import load_settings
from app.downloader import target_filename, validate_download
from app.exceptions import (
    AuthenticationError,
    ConfigurationError,
    DownloadError,
    LockError,
    ReportValidationError,
)
from app.logging_config import RedactingFilter
from app.maintenance import cleanup_old_files
from app.retry import retry_call
from app.runlock import run_lock
from app.selectors import candidates


# -- config -------------------------------------------------------------------


def test_settings_load_and_build_the_entity_urls(settings) -> None:
    assert settings.reports_url_with_entity.endswith("/reports?entityId=ENTITYTEST")
    assert settings.new_report_url_with_entity.endswith("/reports/new?entityId=ENTITYTEST")
    assert "s3cret" not in repr(settings) and "s3cret" not in str(settings.describe())


@pytest.mark.parametrize("missing", ["AMAZON_ENTITY_ID", "DB_PASSWORD"])
def test_required_settings(env, monkeypatch, missing) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigurationError, match=missing):
        load_settings()


def test_entity_can_come_from_the_pasted_link(env, monkeypatch) -> None:
    monkeypatch.delenv("AMAZON_ENTITY_ID")
    monkeypatch.setenv(
        "AMAZON_NEW_REPORT_URL", "https://advertising.amazon.com/reports/new?entityId=ENTITY1YST5GXMAIR36"
    )
    settings = load_settings()
    assert settings.entity_id == "ENTITY1YST5GXMAIR36"
    assert settings.reports_url_with_entity.endswith("?entityId=ENTITY1YST5GXMAIR36")


def test_link_and_entity_must_agree(env, monkeypatch) -> None:
    monkeypatch.setenv("AMAZON_NEW_REPORT_URL", "https://advertising.amazon.com/reports/new?entityId=OTHER")
    with pytest.raises(ConfigurationError, match="same advertiser"):
        load_settings()


def test_credentials_are_both_or_neither(env, monkeypatch) -> None:
    monkeypatch.setenv("AMAZON_EMAIL", "a@b.c")
    with pytest.raises(ConfigurationError, match="AMAZON_PASSWORD"):
        load_settings()


# -- selectors ----------------------------------------------------------------


def test_report_name_is_filled_in_and_escaped() -> None:
    filled = candidates("report_link", params={"report_name": "Bob's report"})
    assert all("{report_name}" not in s for s in filled)
    assert any("Bob\\'s report" in s for s in filled)


# -- download validation --------------------------------------------------------


def test_validate_download(tmp_path: Path) -> None:
    html = tmp_path / "a.xlsx"
    html.write_bytes(b"<!DOCTYPE html><html>" + b"x" * 64)
    with pytest.raises(ReportValidationError, match="HTML"):
        validate_download(html)

    corrupt = tmp_path / "b.xlsx"
    corrupt.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    with pytest.raises(ReportValidationError, match="corrupt"):
        validate_download(corrupt)

    good = tmp_path / "c.zip"
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("r.csv", "Date,Country\n" * 10)
    assert validate_download(good) > 0


def test_target_filename_is_safe_and_stamped() -> None:
    name = target_filename("../Sponsored Products Purchased product report.xlsx")
    assert "/" not in name and " " not in name
    assert name.endswith("_Sponsored_Products_Purchased_product_report.xlsx")


# -- retry --------------------------------------------------------------------


def test_retry_retries_transient_only() -> None:
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise DownloadError("not ready")
        return "ok"

    assert retry_call(flaky, description="x", attempts=3, base_delay=0, max_delay=0) == "ok"

    def locked_out():
        calls.append(1)
        raise AuthenticationError("challenge")

    calls.clear()
    with pytest.raises(AuthenticationError):
        retry_call(locked_out, description="x", attempts=5, base_delay=0, max_delay=0)
    assert len(calls) == 1


# -- lock ---------------------------------------------------------------------


def test_lock_refuses_a_second_holder(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with run_lock(lock):
        with pytest.raises(LockError):
            with run_lock(lock):
                pass
    with run_lock(lock):  # released again
        pass


# -- housekeeping ---------------------------------------------------------------


def test_cleanup_only_touches_owned_old_files(settings) -> None:
    old = settings.download_dir / "20260101T000000Z_report.xlsx"
    new = settings.download_dir / "20261101T000000Z_report.xlsx"
    foreign = settings.download_dir / "keep-me.xlsx"
    for path in (old, new, foreign):
        path.write_bytes(b"x")
    ancient = time.time() - 90 * 86_400
    os.utime(old, (ancient, ancient))
    os.utime(foreign, (ancient, ancient))

    assert cleanup_old_files(settings) == 1
    assert not old.exists() and new.exists() and foreign.exists()


# -- logging ------------------------------------------------------------------


def test_redaction() -> None:
    record = logging.LogRecord(
        "x", logging.INFO, "f", 1,
        "url=%s password=hunter2 next=https://a/?ssoResponse=abc&x=1", ("mysql://u:pw@h/db",), None,
    )
    RedactingFilter().filter(record)
    message = record.getMessage()
    assert "hunter2" not in message and "pw@" not in message and "abc" not in message


# -- CLI ----------------------------------------------------------------------


def test_cli_config_error_exits_2(capsys) -> None:
    from run import main

    assert main(["--dry-run"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_cli_missing_file_exits_2(settings) -> None:
    from run import main

    assert main(["--process-file", "does-not-exist.xlsx"]) == 2

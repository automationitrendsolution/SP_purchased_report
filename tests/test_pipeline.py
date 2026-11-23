"""End-to-end --process-file runs against SQLite (no Amazon, no MySQL)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from app.exceptions import EmptyReportError, LockError
from app.logging_config import LOGGER_NAME
from app.main import ReportPipeline, RunMode, RunOptions
from app.runlock import run_lock
from tests.conftest import make_repository, report_row, write_xlsx


@pytest.fixture
def repo(mapping):
    return make_repository(mapping)


def run(settings, repo, path: Path, *, dry_run: bool = False) -> ReportPipeline:
    pipeline = ReportPipeline(
        settings,
        RunOptions(mode=RunMode.PROCESS_FILE, process_file=path, dry_run=dry_run),
        repository_factory=lambda: repo,
    )
    pipeline.run()
    return pipeline


def test_process_file_loads_and_reloads_without_duplicates(settings, repo, tmp_path) -> None:
    path = write_xlsx(
        tmp_path / "r.xlsx", [report_row(date(2026, 11, d)) for d in (1, 2, 3)]
    )
    first = run(settings, repo, path)
    assert first.summary.succeeded
    assert first.summary.write_result.inserted == 3
    assert (first.summary.first_date, first.summary.last_date) == (date(2026, 11, 1), date(2026, 11, 3))

    second = run(settings, repo, path)
    assert (second.summary.write_result.deleted, second.summary.write_result.inserted) == (3, 3)
    assert repo.count_between(date(2026, 11, 1), date(2026, 11, 3)) == 3


def test_dry_run_writes_nothing(settings, repo, tmp_path) -> None:
    path = write_xlsx(tmp_path / "r.xlsx", [report_row()])
    pipeline = run(settings, repo, path, dry_run=True)
    assert pipeline.summary.write_result is None
    assert repo.count_between(date(2026, 1, 1), date(2026, 12, 31)) == 0


def test_empty_report_deletes_nothing(settings, repo, tmp_path) -> None:
    run(settings, repo, write_xlsx(tmp_path / "full.xlsx", [report_row()]))
    with pytest.raises(EmptyReportError):
        run(settings, repo, write_xlsx(tmp_path / "empty.xlsx", []))
    assert repo.count_between(date(2026, 1, 1), date(2026, 12, 31)) == 1


def test_stale_report_only_warns(settings, repo, tmp_path, caplog, monkeypatch) -> None:
    # The CLI tests configure logging with propagate=False; undo that here.
    monkeypatch.setattr(logging.getLogger(LOGGER_NAME), "propagate", True)
    path = write_xlsx(tmp_path / "r.xlsx", [report_row(date(2020, 1, 1))])
    pipeline = run(settings, repo, path)
    assert pipeline.summary.succeeded
    assert "AMAZON_REPORT_PERIOD" in caplog.text


def test_concurrent_run_is_refused(settings, repo, tmp_path) -> None:
    path = write_xlsx(tmp_path / "r.xlsx", [report_row()])
    with run_lock(settings.lock_file):
        with pytest.raises(LockError):
            run(settings, repo, path)

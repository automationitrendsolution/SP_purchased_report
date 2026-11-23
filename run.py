#!/usr/bin/env python3
"""Command-line entry point.

Creates a Sponsored Products "Purchased product" (Daily) report in the Amazon
Ads console with Playwright, waits until it is ready, downloads it, then loads
it into MySQL, replacing the rows for the dates (per country) it covers.

Modes
-----
    uv run python run.py                               # production run (what cron calls)
    uv run python run.py --headed                      # visible browser, for debugging
    uv run python run.py --dry-run                     # full workflow, no MySQL writes
    uv run python run.py --download-only               # fetch the report and stop
    uv run python run.py --process-file report.xlsx    # load a file without touching Amazon
    uv run python run.py --login                       # one-time Amazon session setup

Running under cron
------------------
cron starts in ``$HOME`` with a near-empty environment, so the job must ``cd``
to the project (``.env`` is read from there) and call ``uv`` by absolute path::

    30 10 * * * cd /opt/sp-purchased-product && /usr/local/bin/uv run --no-sync \\
                python run.py > logs/cron.out 2>&1

Runs never overlap: an exclusive lock on ``LOCK_FILE`` makes a second run exit
11 instead of doubling up.  See docs/setup.md.

Exit codes
----------
    0   success
    2   configuration error
    3   authentication -- run --login, a challenge is waiting
    4   navigation       5  report not found      6  download     12 report form
    7   report file      8  transform             9  database (rolled back)
    10  empty report (nothing written, nothing deleted)
    11  another run holds the lock
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import __version__
from app.config import load_settings
from app.exceptions import ConfigurationError, ReportFlowError
from app.logging_config import configure_logging, get_logger
from app.main import ReportPipeline, RunMode, RunOptions, handle_failure

_HELP_EPILOG = """
examples:
  uv run python run.py                             production run (what cron calls)
  uv run python run.py --headed                    watch it in a visible browser
  uv run python run.py --dry-run                   full workflow, nothing written to MySQL
  uv run python run.py --download-only             fetch the report and stop
  uv run python run.py --process-file report.xlsx  load a file without touching Amazon
  uv run python run.py --login                     one-time sign-in

exit codes:
  0 success · 2 config · 3 authentication (needs a human) · 4 navigation
  5 report not found · 6 download · 7 report file · 8 transform · 9 database
  12 Create report form control not found
  10 empty report (nothing written) · 11 locked by another run
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sp-purchased-product",
        description=(
            "Create, download and load the Sponsored Products 'Purchased product' "
            "report from the Amazon Ads console into MySQL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full workflow without writing to MySQL. Combines with --process-file.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--download-only", action="store_true", help="Download the report and stop."
    )
    modes.add_argument(
        "--process-file",
        type=Path,
        metavar="PATH",
        help="Load an existing .xlsx/.csv/.zip report without opening Amazon.",
    )
    modes.add_argument(
        "--login",
        action="store_true",
        help=(
            "One-time Amazon sign-in by a person. Opens a visible browser when there is "
            "a display; on an SSH-only host asks for the two-step code in this terminal."
        ),
    )
    parser.add_argument(
        "--headed", action="store_true", help="Show the browser window (overrides HEADLESS)."
    )
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override LOG_LEVEL for this run.",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the single-instance lock. Only for manual, supervised runs.",
    )
    parser.add_argument(
        "--echo-sql", action="store_true", help="Log SQL statements (never logs credentials)."
    )
    return parser


def resolve_mode(args: argparse.Namespace) -> RunMode:
    if args.login:
        return RunMode.LOGIN
    if args.process_file is not None:
        return RunMode.PROCESS_FILE
    if args.download_only:
        return RunMode.DOWNLOAD_ONLY
    return RunMode.FULL


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = resolve_mode(args)

    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(
        settings.log_dir,
        level=args.log_level or settings.log_level,
        retention_days=settings.log_retention_days,
    )
    logger = get_logger("cli")

    if args.process_file is not None and not Path(args.process_file).expanduser().is_file():
        logger.error("File not found: %s", args.process_file)
        return 2

    options = RunOptions(
        mode=mode,
        dry_run=args.dry_run,
        headed=True if args.headed else None,
        process_file=args.process_file,
        use_lock=not args.no_lock,
        echo_sql=args.echo_sql,
    )
    pipeline = ReportPipeline(settings, options)
    try:
        pipeline.run()
    except ReportFlowError as exc:
        handle_failure(pipeline, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception:  # noqa: BLE001 - already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

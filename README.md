# Sponsored Products — Purchased product report → MySQL

Downloads the Amazon Ads **Sponsored Products › Purchased product** report from
the Ads console with Playwright, and loads it into the MySQL table
`sponsored_products_purchased_product_report`. It doesn't use the Amazon Ads API.

```
Reports list ──▶ Create report ──Playwright──▶ Run report ──▶ reload Reports until ready ──▶ .xlsx
        ──pandas──▶ clean / type / map ──▶ MySQL (replace covered dates, one transaction)
```

The project follows the structure and safeguards of `out-of-budget-us`.

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [One-time: sign in](#one-time-sign-in)
- [Running it](#running-it)
- [The database](#the-database)
- [Scheduling](#scheduling)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security](#security)

## How it works

1. **Create** (`app/amazon_ads.py`). Chromium reuses the saved session in
   `data/session/`, opens `AMAZON_REPORTS_URL` (the Reports list), clicks **Create report**
   (falling back to `AMAZON_NEW_REPORT_URL` if the button is not found), then:
   1. chooses Report category **Sponsored Products**, Report type **Purchased
      product** and Time unit **Daily**, plus `AMAZON_REPORT_PERIOD` if you set one;
   2. names the report `AMAZON_REPORT_NAME_PREFIX` plus a UTC timestamp, for
      example `SP Purchased product auto 20261123T183000Z`;
   3. clicks **Run report**.
2. **Wait and download** (`app/downloader.py`):
   1. Reloads the Reports list every `REPORT_POLL_SECONDS` (default 60 s) until
      the report it just created shows *Download*, for up to
      `REPORT_READY_TIMEOUT_MS` (default 60 min; Amazon took ~35 min on 2026-11-23).
   2. Downloads exactly that report. Older reports in the list are ignored.
   3. If the download step fails and is retried, it goes back to waiting for
      the same report. It never creates a second one in the same run.
3. **Read** (`app/report_processor.py`): accepts .xlsx, .csv or a .zip of
   either. It finds the header row and checks the required columns.
4. **Transform** (`app/data_transformer.py`):
   1. Types each column by its Amazon name, so `$1,204.55` becomes 1204.55 and
      Amazon's `-` becomes NULL. Campaign names are never number-cleaned.
   2. Parses dates.
   3. Renames columns using `config/Purchased_Product_Heading.csv`.
   4. Aligns the result to the live table.
5. **Load** (`app/database.py`): one transaction that, for each country and the
   first-to-last date the report covers, `DELETE`s the existing rows, then
   `INSERT`s the report's rows and commits.
   - Re-running the same report never duplicates anything.
   - Amazon's late attribution updates replace the older figures.
   - Other countries are never touched.
   - An empty or broken report deletes nothing.

## Installation

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                     # creates .venv from uv.lock
uv run playwright install --with-deps chromium   # Linux; on Windows/macOS drop --with-deps
cp .env.example .env && chmod 600 .env      # then fill it in
```

### Report period

By default the pipeline leaves Amazon's Report period as it is. To choose one,
set `AMAZON_REPORT_PERIOD` to the label exactly as the console shows it, for
example `Last 30 days`. Any period works with the loader, because it replaces
exactly the dates in the file. A longer window means each run also picks up
Amazon's late attribution changes.

Each run adds one report to your Reports list in the Ads console. Amazon keeps
them; delete old ones there if the list gets long.

## One-time: sign in

```bash
uv run python run.py --login
```

- **On a machine with a display:** a browser window opens. Finish anything
  Amazon asks for, then press Enter.
- **On an SSH-only server:** Chromium stays headless and you type the two-step
  code at the prompt.

Either way the session is saved in `data/session/`, so scheduled runs don't see
a login form. If the session expires, the run fills in `AMAZON_EMAIL` and
`AMAZON_PASSWORD` itself.

A scheduled run **never** answers a CAPTCHA or OTP prompt. It stops with exit
code 3, and you run `--login` again.

## Running it

```bash
uv run python run.py                                   # production run (what cron calls)
uv run python run.py --headed                          # watch the browser
uv run python run.py --dry-run                         # everything except the MySQL write
uv run python run.py --download-only                   # fetch the report and stop
uv run python run.py --process-file report.xlsx        # load a file you downloaded yourself
uv run python run.py --process-file report.xlsx --dry-run
```

### Exit codes

| Code | Meaning | Action |
|---|---|---|
| 0 | success | — |
| 2 | configuration error | fix `.env` |
| 3 | authentication: sign-in, CAPTCHA or OTP needed | `run.py --login` |
| 4 | Reports page did not load | retried automatically; check the screenshot |
| 5 | the created report never appeared in the list | check the screenshot and `report_row` in `selectors.json` |
| 12 | a Create report control was not found | retried automatically; then update the key named in `selectors.json` |
| 6 | download failed or report not ready | retried automatically |
| 7 | file is not a valid report | check `data/failed/` |
| 8 | transform error | check the log |
| 9 | database error | the transaction was rolled back and the data is intact |
| 10 | empty report | nothing was deleted or inserted |
| 11 | another run holds the lock | — |

Every run ends with a SUCCESS/FAILED summary block in `logs/pipeline.log`.

## The database

The table already exists in `gen_ai_db`. The pipeline reflects it and never
alters it. `CREATE_TABLES=true` creates the table only when it is missing; the
DDL is in [docs/setup.md](docs/setup.md#appendix-table-ddl).

- `id` (auto-increment) and `loaded_at` (`DEFAULT CURRENT_TIMESTAMP`) are filled
  by the server.
- If several brands or accounts ever share the table, add a `brand_code` column
  and set `BRAND_CODE`. Deletes are then narrowed to that brand, and the
  pipeline refuses to run if the column exists but `BRAND_CODE` is not set.

## Scheduling

Run it once a day. Each run creates and waits for its own report, so the time of day only decides how current "yesterday" is. See
[docs/setup.md](docs/setup.md#7-schedule-it) for the cron entry and a systemd
timer. Overlapping runs are refused by an internal lock, so no `flock` wrapper
is needed.

## Testing

```bash
uv run pytest            # offline: SQLite + mock Reports pages in headless Chromium
uv run pytest -m live    # read-only checks against the real MySQL and Ads console
```

## Troubleshooting

- **`AuthenticationError` (3).** Amazon wants a person. Run `uv run python run.py --login`.
- **`ReportBuildError` (12).** A Create report choice (category, type, time
  unit, name, Run report) wasn't found. The error names the selector key; look
  at `data/screenshots/` and add a working selector at the top of that key.
- **`ReportNotFoundError` (5).** The report this run created never showed up in
  the list. Either Run report didn't submit (check the Reports list in the
  console), or the list markup differs from the guesses in `report_row`.
- **`DownloadError` naming a selector key.** Same fix: update that key in
  `selectors.json`.
- **"The newest date in this report is … days before today".** The console
  report period you chose ends earlier than expected. The run still succeeds
  because reloading old data is harmless.
- **`DatabaseError` (9).** The transaction was rolled back and existing rows are
  intact. Connection drops and deadlocks are retried; anything else is in the log.

## Security

- `.env` and `data/session/` hold secrets and are git-ignored. Keep both
  `chmod 600` / `700`.
- Passwords, cookies and SSO tokens are scrubbed from every log line, and
  logged URLs have their query strings stripped.
- Downloaded reports contain client data and are git-ignored. They are deleted
  after `FILE_RETENTION_DAYS`.
#   S P _ p u r c h a s e d _ r e p o r t  
 
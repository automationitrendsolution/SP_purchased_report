# SP Purchased Product Report → MySQL

Every day, this tool downloads the Amazon Ads **Sponsored Products – Purchased product** report and saves it into our MySQL database. Nobody has to download or upload anything by hand.

> ⚠️ **Amazon is retiring the report page this tool uses.** From **17 December 2026**, new reports can't be created there, and it closes on **31 January 2027**. The tool must be moved to Amazon's new *Measurement & reporting › Reporting* page before 17 December, or the daily run will start failing.

---

## Contents

1. [What it does](#1-what-it-does)
2. [Where the data goes](#2-where-the-data-goes)
3. [First-time setup](#3-first-time-setup)
4. [Running it](#4-running-it)
5. [Running it automatically every day](#5-running-it-automatically-every-day)
6. [Checking that it worked](#6-checking-that-it-worked)
7. [When something goes wrong](#7-when-something-goes-wrong)
8. [Good to know](#8-good-to-know)
9. [For developers](#9-for-developers)

---

## 1. What it does

Each run does what a person would do in the Amazon Ads console, then saves the result:

| Step | What happens | Time |
|---|---|---|
| 1. Open Amazon Ads | Opens the **Reports** page, already signed in | seconds |
| 2. Create the report | Clicks **Create report**, chooses **Sponsored Products → Purchased product → Daily**, clicks **Run report** | seconds |
| 3. Wait for Amazon | Checks the report's page every minute until Amazon has finished generating it | **about 35 minutes** |
| 4. Download | Downloads the finished Excel file | seconds |
| 5. Save to the database | Replaces the days in the report with the new figures | about 10 seconds |

**The report always covers the last 30 days.** Every run refreshes those 30 days, so late purchases Amazon adds afterwards are picked up automatically (see [Good to know](#8-good-to-know)).

**A run never creates duplicates.** Days that are already in the database are replaced, not added again. Running it twice in a row is safe.

---

## 2. Where the data goes

| | |
|---|---|
| Database | `gen_ai_db` (MySQL) |
| Table | `sponsored_products_purchased_product_report` |
| One row is | one date + campaign + ad group + advertised product + purchased product |

| Column | Meaning |
|---|---|
| `report_date` | the day the ad was clicked |
| `portfolio_name`, `campaign_name`, `ad_group_name` | where the ad ran |
| `country`, `currency`, `retailer` | marketplace (currently United States, USD, Amazon) |
| `advertised_sku`, `advertised_asin` | the product that was advertised |
| `targeting`, `match_type` | how the ad was targeted (`match_type` is empty for automatic/category targeting) |
| `purchased_asin` | the **other** product the shopper actually bought |
| `other_sku_units_7d`, `other_sku_orders_7d`, `other_sku_sales_7d` | units, orders and sales of that other product, within 7 days of the click |
| `loaded_at` | when the row was saved (UTC) |

---

## 3. First-time setup

You only do this once per computer or server.

**You need:** Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/) (the tool that installs everything else).

**Step 1: install.** Run this in the project folder:

```bash
uv sync
uv run playwright install chromium        # on Linux servers: add --with-deps
```

**Step 2: settings.** Copy `.env.example` to `.env` and fill in:

| Setting | What to put |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | the MySQL login |
| `AMAZON_REPORTS_URL` | the Reports page link, including `?entityId=...` for the right account |
| `AMAZON_ENTITY_ID` | the same `entityId` value |
| `AMAZON_EMAIL`, `AMAZON_PASSWORD` | the Amazon Ads login, used if the saved sign-in expires |

Everything else in `.env` can stay as it is. `.env` contains passwords: never share it or commit it.

**Step 3: sign in to Amazon once.**

```bash
uv run python run.py --login
```

Enter the two-step verification code if Amazon asks for one. The sign-in is then remembered, and daily runs don't need it again.

> On a Linux server, run `--login` **on the server itself**. A sign-in saved on a Windows PC doesn't work on Linux.

---

## 4. Running it

Open a terminal in the project folder and use one of these:

| I want to… | Command |
|---|---|
| **Run it for real** (download and save to the database) | `uv run python run.py` |
| Run it and watch the browser | `uv run python run.py --headed` |
| Test everything **without** saving to the database | `uv run python run.py --dry-run` |
| Only download the report (no database) | `uv run python run.py --download-only` |
| Save a report file I downloaded myself | `uv run python run.py --process-file "path\to\report.xlsx"` |
| Sign in to Amazon again | `uv run python run.py --login` |

A full run takes about **40 minutes**, mostly waiting for Amazon. It's done when you see `status : SUCCESS`.

---

## 5. Running it automatically every day

Run it once a day. Pick a time when the previous day is complete in the US, for example **10:30 US Pacific time** (18:30 UTC, or midnight IST).

**On a Linux server (cron).** Add this line with `crontab -e`, adjusting the folder path:

```cron
30 18 * * * cd /opt/sp-purchased-product && /home/reports/.local/bin/uv run --no-sync python run.py > logs/cron.out 2>&1
```

**On a Windows PC (Task Scheduler).** Create a daily task that runs `uv run python run.py`, with *Start in* set to the project folder. The PC must be switched on at that time.

If a run is still going when the next one starts, the new one stops straight away, so runs never overlap.

Full server setup, step by step: [docs/setup.md](docs/setup.md).

---

## 6. Checking that it worked

- **The log file:** `logs/pipeline.log`. Every run ends with a summary block like this:
  ```
  Run summary (full)
    dates covered      : 2026-10-24 .. 2026-11-22
    rows processed     : 14483
    sponsored_products_purchased_product_report: 14482 deleted, 14483 inserted
    status             : SUCCESS
  ```
- **The downloaded file** is kept in `data/processed/` for 30 days.
- **In the database:** the latest date should be **yesterday**:
  ```sql
  SELECT MAX(report_date), COUNT(*) FROM sponsored_products_purchased_product_report;
  ```

---

## 7. When something goes wrong

The run stops with an exit code and a clear message in `logs/pipeline.log`. If the browser was involved, a screenshot of what it saw is saved in `data/screenshots/`.

| Exit code | What it means | What to do |
|---|---|---|
| **0** | Success | Nothing |
| **2** | A setting in `.env` is missing or wrong | Fix the setting named in the message |
| **3** | Amazon wants a sign-in code or CAPTCHA | Run `uv run python run.py --login` |
| **4, 6, 12** | An Amazon page was slow or looked different | Already retried automatically. If it keeps happening, send the screenshot to a developer |
| **5** | The report was created but never showed up | Check the Reports page in the Amazon console; send the screenshot to a developer |
| **7** | The downloaded file isn't a valid report | The file is kept in `data/failed/`; send it to a developer |
| **9** | The database had a problem | Nothing was changed in the database. Check the database is reachable, then run again |
| **10** | The report was empty | Nothing was deleted or saved. Run again later |
| **11** | Another run is still in progress | Nothing; wait for it to finish |

**Safe by design:** if anything fails, the database keeps its existing data. Rows are only replaced when the new report has been downloaded and checked successfully.

---

## 8. Good to know

- **Recent days still go up.** The figures are *7-day* figures: a purchase is credited to an ad click up to 7 days later. So the last few days in the table are provisional and grow over the following week. The daily run refreshes them automatically, and days older than about a week are final.
- **Each run leaves a report in Amazon.** Every run adds one report named like `SP Purchased product auto 20261123T113109Z` to the Reports list in the Amazon console. They can be deleted there if the list gets long.
- **Other countries are safe.** A run only replaces rows for the country and dates in its own report.
- **The sign-in is a secret.** `data/session/` holds the remembered Amazon sign-in, so treat it like a password.

---

## 9. For developers

<details>
<summary>Project layout, how the code works, tests, and how to fix a changed Amazon page</summary>

### Project layout

```
run.py                       command-line entry point (what cron calls)
app/
  amazon_ads.py              Amazon pages: Reports list, Create report form, report history page
  auth.py, browser.py        sign-in and Chromium lifecycle (saved session in data/session/)
  downloader.py              captures and validates the downloaded file
  report_processor.py        reads .xlsx/.csv/.zip, finds the header, checks required columns
  data_transformer.py        cleans and types values, works out which dates to replace
  database.py                MySQL: one transaction = DELETE covered dates + INSERT
  main.py                    orchestration, retries, run summary
  config.py, schema.py, selectors.py, retry.py, runlock.py, maintenance.py, logging_config.py
config/
  selectors.json             how each button/field on the Amazon pages is found
  Purchased_Product_Heading.csv   Amazon column name -> MySQL column name
docs/setup.md                Linux server setup, cron/systemd, table DDL
tests/                       offline tests; tests/live/ = read-only checks against real systems
data/                        downloads, processed/failed files, screenshots, session (all git-ignored)
```

### How a run works

1. `ReportsPage.open_builder()` opens `AMAZON_REPORTS_URL` and clicks **Create report** (falls back to `AMAZON_NEW_REPORT_URL`).
2. `create_report()` sets each dropdown only if it doesn't already show the right value (an opened menu covers the form and blocks later clicks), checks **Daily**, names the report `AMAZON_REPORT_NAME_PREFIX` plus a UTC timestamp, and clicks **Run report**.
3. `wait_until_downloadable()` finds the report's link in the list, then reloads its history page (`/reports/history/<id>`) every `REPORT_POLL_SECONDS` (60 s) for up to `REPORT_READY_TIMEOUT_MS` (60 min) until a run shows **Download**. The list itself has no Download. The history page's own Run report button is never clicked.
4. If the browser stage fails after Run report, the retry waits for the **same** report; a second report is never created in one run.
5. The file is validated, typed and mapped, then loaded in one transaction: for each country, `DELETE` the first-to-last date in the file, `INSERT` the rows, `COMMIT`. A failure rolls back, and an empty report deletes nothing.

### Optional settings

| Setting | Default | Purpose |
|---|---|---|
| `AMAZON_REPORT_PERIOD` | unset (Amazon's default, Last 30 days) | choose a different Report period, written exactly as the console shows it |
| `AMAZON_REPORT_NAME_PREFIX` | `SP Purchased product auto` | prefix of each run's report name |
| `REPORT_READY_TIMEOUT_MS` / `REPORT_POLL_SECONDS` | 3600000 / 60 | how long and how often to wait for Amazon |
| `HEADLESS` | `true` | `false` shows the browser |
| `MAX_RETRIES` | `3` | attempts for temporary failures (sign-in problems are never retried) |
| `KEEP_PROCESSED_FILES` / `FILE_RETENTION_DAYS` | `true` / 30 | keep loaded files, and for how long |
| `BRAND_CODE` | unset | only if several brands share the table **and** it has a `brand_code` column |
| `CREATE_TABLES` | `false` | create the table if it's missing (an existing table is never altered) |

Full list with comments: [.env.example](.env.example).

### When Amazon changes a page

Buttons and fields are found using the lists in [config/selectors.json](config/selectors.json). When a run fails naming a key (for example `report_type_trigger`), open the screenshot in `data/screenshots/`, then add a working selector at the **top** of that key's list. No code change is needed. See [config/README.md](config/README.md).

### Tests

```bash
uv run pytest            # offline: SQLite + a mock Amazon console in headless Chromium
uv run pytest -m live    # read-only checks against the real MySQL and Amazon (never creates a report)
```

### Security

- `.env` and `data/session/` hold secrets and are git-ignored.
- Passwords, cookies and sign-in tokens are removed from every log line.
- Downloaded reports contain client data, are git-ignored, and are deleted after `FILE_RETENTION_DAYS`.

</details>

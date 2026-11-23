# `config/`

## `Purchased_Product_Heading.csv` — column mapping

Maps each Amazon report header to the MySQL column it is written to.

```csv
Old_Column_Name,New_Column_Name
Campaign Name,campaign_name
```

`Date` and `Country` must be mapped: the loader uses them to decide which
existing rows a report replaces. Renaming a MySQL column is a mapping edit, not
a code change. The live table is reflected at runtime and any mismatch is
logged, so check a change without writing anything:

```bash
uv run python run.py --process-file data/samples/purchased_product_sample.csv --dry-run
```

## `selectors.json` — Amazon UI locators

Ordered candidate selectors for each element the pipeline interacts with. The
earliest-listed one that is visible wins. `{report_name}` is replaced with the
run's report name, and `{value}` with the dropdown value being set (e.g.
`Purchased product`, or `AMAZON_REPORT_PERIOD`).

* The **sign-in / OTP / CAPTCHA** entries came from the `out-of-budget-us`
  project, where they were checked against the live Amazon sign-in pages.
* The **Reports list**, **Create report** and **report history** entries were
  verified against the live console on 2026-11-23: a full run created, waited
  for, downloaded and loaded a report using them.
* Not yet exercised live: `report_period_*` (only used if `AMAZON_REPORT_PERIOD`
  is set), `report_category_trigger` / `report_category_option` (only used if
  Report category is ever not already Sponsored Products), and
  `create_report_button`'s fallback path.

When a run fails with a `NavigationError`, `ReportBuildError`,
`ReportNotFoundError` or `DownloadError` naming a selector key, open the screenshot in
`data/screenshots/`, then add a working selector at the **top** of that key's
list. You don't need to change code or redeploy.

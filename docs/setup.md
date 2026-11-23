# Server setup (Linux)

How to install the pipeline on a Linux server and run it daily with cron or a
systemd timer. The paths assume `/opt/sp-purchased-product` and a service user
`reports`; adjust both as needed.

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

Chromium's own libraries are installed in step 3 by `playwright install --with-deps`.

## 2. Service user and project directory

```bash
sudo useradd --system --create-home --shell /bin/bash reports
sudo mkdir -p /opt/sp-purchased-product
sudo chown reports:reports /opt/sp-purchased-product
sudo -iu reports
cd /opt/sp-purchased-product
# copy or clone the project here
```

## 3. uv, dependencies and Chromium

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # installs to ~/.local/bin/uv
~/.local/bin/uv sync --frozen                         # exact versions from uv.lock
sudo ~/.local/bin/uv run playwright install-deps chromium
~/.local/bin/uv run playwright install chromium
```

## 4. Configuration (`.env`)

```bash
cp .env.example .env
chmod 600 .env
```

Fill in `DB_*`, `AMAZON_REPORTS_URL` (the Reports page link with
`?entityId=...`), `AMAZON_ENTITY_ID`, and (recommended) `AMAZON_EMAIL` /
`AMAZON_PASSWORD`. The runtime directories under `data/` and `logs/` are created
on first run.

## 5. Amazon sign-in (once, by hand)

```bash
~/.local/bin/uv run python run.py --login
```

Over plain SSH, Chromium stays headless and asks for the two-step code in the
terminal.

The sign-in must be made **on the server itself**. A `data/session/` folder
copied from a Windows PC does not work on Linux, because Chrome encrypts the
saved cookies with a key tied to the machine. If Amazon shows an image CAPTCHA
(which a terminal can't answer), connect with X forwarding (`ssh -X`) and run
`--login --headed` to get a browser window, or copy `data/session/` from
**another Linux** machine where `--login` succeeded.

```bash
chmod 700 data/session
```

## 6. Verify without writing to MySQL

```bash
~/.local/bin/uv run python run.py --dry-run
```

This is a complete run except for the MySQL write. It **does** create a report
in the Amazon console and waits for it (about 35–40 minutes). For a quick
check that doesn't create anything, use the read-only live tests:
`uv run pytest -m live`. If a run fails naming a selector key, see
`config/README.md`.

## 7. Schedule it

Each run creates its own report and waits up to `REPORT_READY_TIMEOUT_MS` for it,
so allow for that in timeouts. The examples use 18:30 UTC, which is 10:30
US/Pacific, when the previous day's US data is normally complete.

### Option A: cron

`crontab -e` as the `reports` user:

```cron
# m  h  dom mon dow  command
30 18 *   *   *    cd /opt/sp-purchased-product && /home/reports/.local/bin/uv run --no-sync python run.py > logs/cron.out 2>&1
```

* `cd` first, because `.env` is read from the project directory.
* `uv` is called by absolute path, because cron's `PATH` is minimal.
* `--no-sync` stops a scheduled run from changing installed packages.
* `> logs/cron.out` keeps only the latest run's console output (useful if
  Python itself crashes); the full history is in the rotating `logs/pipeline.log`.
* No `flock` wrapper is needed; the pipeline holds its own lock (exit 11 if
  another run is still going).

### Option B: systemd timer

`/etc/systemd/system/sp-purchased-product.service`:

```ini
[Unit]
Description=SP Purchased product report -> MySQL
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=reports
WorkingDirectory=/opt/sp-purchased-product
ExecStart=/home/reports/.local/bin/uv run --no-sync python run.py
TimeoutStartSec=45min
```

`/etc/systemd/system/sp-purchased-product.timer`:

```ini
[Unit]
Description=Daily SP Purchased product report load

[Timer]
OnCalendar=*-*-* 18:30:00 UTC
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sp-purchased-product.timer
systemctl list-timers sp-purchased-product.timer
journalctl -u sp-purchased-product.service -n 100
```

## 8. Monitoring

Alert on a non-zero exit code. Exit code **3** is the only one that needs a
person (run `--login`); the others either retry on their own or point to a
config fix. Each run ends with a SUCCESS/FAILED summary block in
`logs/pipeline.log`.

## 9. Housekeeping (automatic)

* `logs/pipeline.log` rotates at UTC midnight and keeps `LOG_RETENTION_DAYS` files.
* Downloads, archived reports and screenshots are deleted after
  `FILE_RETENTION_DAYS`. Only files the pipeline created (UTC-timestamp prefix)
  are ever deleted.

## Appendix: table DDL

This is the live table in `gen_ai_db`. `CREATE_TABLES=true` produces the same
structure when the table is missing.

```sql
CREATE TABLE `sponsored_products_purchased_product_report` (
  `id` int NOT NULL AUTO_INCREMENT,
  `report_date` date NOT NULL,
  `portfolio_name` varchar(255) DEFAULT NULL,
  `campaign_name` varchar(255) DEFAULT NULL,
  `country` varchar(64) DEFAULT NULL,
  `currency` varchar(8) DEFAULT NULL,
  `ad_group_name` varchar(255) DEFAULT NULL,
  `retailer` varchar(64) DEFAULT NULL,
  `advertised_sku` varchar(128) DEFAULT NULL,
  `advertised_asin` varchar(20) DEFAULT NULL,
  `targeting` varchar(512) DEFAULT NULL,
  `match_type` varchar(32) DEFAULT NULL,
  `purchased_asin` varchar(20) DEFAULT NULL,
  `other_sku_units_7d` int DEFAULT NULL,
  `other_sku_orders_7d` int DEFAULT NULL,
  `other_sku_sales_7d` decimal(14,2) DEFAULT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_report_date` (`report_date`),
  KEY `idx_advertised_asin` (`advertised_asin`),
  KEY `idx_purchased_asin` (`purchased_asin`),
  KEY `idx_country_date` (`country`, `report_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

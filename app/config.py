"""Environment-driven configuration.

Every secret arrives through the process environment (populated from ``.env``
locally, or from real environment variables / a secrets manager in
production).  Validation is strict and fails fast: a run that starts with a bad
DSN should never get as far as opening a browser.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError
from .schema import DEFAULT_TABLE

PROJECT_ROOT = Path(__file__).resolve().parent.parent

UTC = ZoneInfo("UTC")

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false), got {raw!r}")


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_path(name: str, default: Path) -> Path:
    raw = _get(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


#: The default /dev/shm on small cloud instances is 64 MB, which Chromium
#: exhausts and then crashes.
_DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)


def _browser_args() -> tuple[str, ...]:
    """Chromium flags: the server-safe defaults plus anything in ``BROWSER_ARGS``."""
    args = list(_DEFAULT_BROWSER_ARGS)
    extra = _get("BROWSER_ARGS")
    if extra:
        args.extend(part.strip() for part in extra.split(",") if part.strip())
    if _get_bool("BROWSER_NO_SANDBOX", False):
        args.append("--no-sandbox")
    return tuple(dict.fromkeys(args))


def _get_zone(name: str, default: str) -> ZoneInfo:
    raw = _get(name, default) or default
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{name}={raw!r} is not a valid IANA timezone. "
            "Install the 'tzdata' package if the system zone database is missing."
        ) from exc


_ENTITY_PARAM = re.compile(r"[?&]entityId=([A-Za-z0-9]+)")


def _entity_from(url: str) -> str | None:
    match = _ENTITY_PARAM.search(url or "")
    return match.group(1) if match else None


def _with_entity(url: str, entity_id: str) -> str:
    if _entity_from(url):
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}entityId={entity_id}"


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 30
    #: Path to a CA bundle when the server requires TLS.
    ssl_ca: Path | None = None
    ssl_verify_cert: bool = False
    #: Disable TLS entirely.  Only for a server on a trusted private network.
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        """Driver kwargs for mysql-connector-python."""
        args: dict[str, object] = {
            "connection_timeout": self.connect_timeout_seconds,
            "charset": "utf8mb4",
        }
        if self.ssl_disabled:
            args["ssl_disabled"] = True
            return args
        if self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        """SQLAlchemy URL.  Credentials are percent-encoded, never logged."""
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def safe_url(self) -> str:
        """Same URL with the password redacted -- this is the loggable form."""
        return f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    #: The Reports list, polled until the created report can be downloaded.
    reports_url: str
    #: Create report (``/reports/new``), filled in on every run.
    new_report_url: str
    entity_id: str
    #: Each run's report is named "<prefix> <UTC timestamp>" so it can be found again.
    report_name_prefix: str
    #: Optional Report period to pick (e.g. "Last 30 days"); None keeps Amazon's default.
    report_period: str | None
    #: How long to keep reloading the list for the report to become downloadable.
    report_ready_timeout_ms: int
    report_poll_seconds: float
    database: DatabaseConfig

    #: Optional: without them the pipeline relies purely on the saved session
    #: and asks for a human once it expires.
    amazon_email: str | None
    amazon_password: str | None = field(repr=False)
    auto_login: bool

    download_dir: Path
    processed_dir: Path
    failed_dir: Path
    log_dir: Path
    screenshot_dir: Path
    session_dir: Path
    lock_file: Path
    heading_map_file: Path

    headless: bool
    browser_timeout_ms: int
    download_timeout_ms: int
    slow_mo_ms: int
    user_agent: str | None
    browser_args: tuple[str, ...]
    viewport_width: int
    viewport_height: int

    #: The advertiser's reporting zone.  Chromium runs in it, and "how old is
    #: this report" is measured in it -- never in the server's local time.
    report_timezone: ZoneInfo
    #: Warn when the newest date in a download is older than this many days:
    #: the chosen report period probably ends earlier than intended.
    report_max_age_days: int

    table: str
    #: Optional brand/account scope for a table shared by several accounts.
    brand_code: str | None

    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float

    create_tables: bool
    retention_days: int
    log_level: str
    log_retention_days: int
    keep_processed_files: bool = False

    @property
    def scope(self) -> dict[str, str]:
        """Configured values for the optional shared-table scope columns."""
        return {"brand_code": self.brand_code} if self.brand_code else {}

    @property
    def has_credentials(self) -> bool:
        return bool(self.amazon_email and self.amazon_password)

    @property
    def can_auto_login(self) -> bool:
        return self.auto_login and self.has_credentials

    @property
    def reports_url_with_entity(self) -> str:
        """Reports list URL carrying the configured advertiser entity."""
        return _with_entity(self.reports_url, self.entity_id)

    @property
    def new_report_url_with_entity(self) -> str:
        """Create report URL carrying the configured advertiser entity."""
        return _with_entity(self.new_report_url, self.entity_id)

    def ensure_directories(self) -> None:
        """Create every directory the pipeline writes to.  Idempotent."""
        for path in (
            self.download_dir,
            self.processed_dir,
            self.failed_dir,
            self.log_dir,
            self.screenshot_dir,
            self.session_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "new_report_url": self.new_report_url,
            "reports_url": self.reports_url,
            "entity_id": self.entity_id,
            "report_period": self.report_period or "(Amazon default)",
            "ready_timeout_s": self.report_ready_timeout_ms // 1000,
            "credentials": "configured" if self.has_credentials else "not configured",
            "auto_login": self.can_auto_login,
            "database": self.database.safe_url,
            "table": self.table,
            "scope": self.scope or "(none configured)",
            "download_dir": str(self.download_dir),
            "session_dir": str(self.session_dir),
            "headless": self.headless,
            "report_timezone": str(self.report_timezone),
            "max_retries": self.max_retries,
            "keep_processed_files": self.keep_processed_files,
        }


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Args:
        env_file: Optional explicit ``.env`` path.  Defaults to ``.env`` in the
            project root when it exists.
        override: Whether ``.env`` values replace already-exported variables.
            Off by default so that real environment variables win.

    Raises:
        ConfigurationError: If a required value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    ssl_ca_raw = _get("DB_SSL_CA")
    ssl_ca = None
    if ssl_ca_raw:
        ssl_ca = Path(ssl_ca_raw).expanduser()
        if not ssl_ca.is_absolute():
            ssl_ca = (PROJECT_ROOT / ssl_ca).resolve()
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    database = DatabaseConfig(
        host=_require("DB_HOST"),
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=_require("DB_NAME"),
        user=_require("DB_USER"),
        password=_require("DB_PASSWORD"),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )

    url = _get("AMAZON_REPORTS_URL", "https://advertising.amazon.com/reports") or ""
    new_url = _get("AMAZON_NEW_REPORT_URL", "https://advertising.amazon.com/reports/new") or ""
    for key, value in (("AMAZON_REPORTS_URL", url), ("AMAZON_NEW_REPORT_URL", new_url)):
        if not value.startswith("https://"):
            raise ConfigurationError(f"{key} must be an https URL, got {value!r}")

    # The entity can come from AMAZON_ENTITY_ID or from the entityId=... already
    # in a pasted console URL; if both are given they must agree.
    entity = _get("AMAZON_ENTITY_ID") or _entity_from(new_url) or _entity_from(url)
    if not entity:
        raise ConfigurationError(
            "Required environment variable AMAZON_ENTITY_ID is not set (and no "
            "entityId=... is present in AMAZON_NEW_REPORT_URL / AMAZON_REPORTS_URL)"
        )
    for key, value in (("AMAZON_REPORTS_URL", url), ("AMAZON_NEW_REPORT_URL", new_url)):
        embedded = _entity_from(value)
        if embedded and embedded != entity:
            raise ConfigurationError(
                f"{key} has entityId={embedded} but AMAZON_ENTITY_ID is {entity}; "
                "they must be the same advertiser account"
            )

    heading_map = _get_path(
        "HEADING_MAP_FILE", PROJECT_ROOT / "config" / "Purchased_Product_Heading.csv"
    )
    if not heading_map.is_file():
        raise ConfigurationError(f"Column mapping file not found: {heading_map}")

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    settings = Settings(
        reports_url=url,
        new_report_url=new_url,
        entity_id=entity,
        report_name_prefix=_get("AMAZON_REPORT_NAME_PREFIX", "SP Purchased product auto")
        or "SP Purchased product auto",
        report_period=_get("AMAZON_REPORT_PERIOD"),
        report_ready_timeout_ms=_get_int("REPORT_READY_TIMEOUT_MS", 3_600_000, minimum=10_000),
        report_poll_seconds=_get_float("REPORT_POLL_SECONDS", 60.0, minimum=0.1),
        amazon_email=_get("AMAZON_EMAIL"),
        amazon_password=_get("AMAZON_PASSWORD"),
        auto_login=_get_bool("AMAZON_AUTO_LOGIN", True),
        database=database,
        download_dir=_get_path("DOWNLOAD_DIR", data_dir / "downloads"),
        processed_dir=_get_path("PROCESSED_DIR", data_dir / "processed"),
        failed_dir=_get_path("FAILED_DIR", data_dir / "failed"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        screenshot_dir=_get_path("SCREENSHOT_DIR", data_dir / "screenshots"),
        session_dir=_get_path("SESSION_DIR", data_dir / "session"),
        lock_file=_get_path("LOCK_FILE", data_dir / "pipeline.lock"),
        heading_map_file=heading_map,
        headless=_get_bool("HEADLESS", True),
        browser_timeout_ms=_get_int("BROWSER_TIMEOUT_MS", 60_000, minimum=1_000),
        download_timeout_ms=_get_int("DOWNLOAD_TIMEOUT_MS", 180_000, minimum=1_000),
        slow_mo_ms=_get_int("SLOW_MO_MS", 0, minimum=0),
        user_agent=_get("BROWSER_USER_AGENT"),
        browser_args=_browser_args(),
        viewport_width=_get_int("VIEWPORT_WIDTH", 1920, minimum=320),
        viewport_height=_get_int("VIEWPORT_HEIGHT", 1080, minimum=320),
        report_timezone=_get_zone("REPORT_TIMEZONE", "US/Pacific"),
        report_max_age_days=_get_int("REPORT_MAX_AGE_DAYS", 3, minimum=1),
        table=_get("DB_TABLE", DEFAULT_TABLE) or DEFAULT_TABLE,
        brand_code=_get("BRAND_CODE"),
        max_retries=_get_int("MAX_RETRIES", 3, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 5.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        create_tables=_get_bool("CREATE_TABLES", False),
        retention_days=_get_int("FILE_RETENTION_DAYS", 30, minimum=1),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 14, minimum=1),
        keep_processed_files=_get_bool("KEEP_PROCESSED_FILES", True),
    )

    if bool(settings.amazon_email) != bool(settings.amazon_password):
        missing = "AMAZON_PASSWORD" if settings.amazon_email else "AMAZON_EMAIL"
        raise ConfigurationError(
            f"{missing} is not set. Automatic sign-in needs both AMAZON_EMAIL and "
            "AMAZON_PASSWORD, or neither (session-only mode)."
        )
    return settings

"""Amazon Ads console automation: create the report, wait for it, find Download.

Drives the public web UI exactly as a person would:

1. open **Create report** (``/reports/new``) for the configured entity;
2. choose Report category *Sponsored Products*, Report type *Purchased
   product*, Time unit *Daily* (and, if configured, a Report period);
3. give the report a unique name and click **Run report**;
4. reload the **Reports** list until that report's Download link appears.

Security boundaries this module will not cross:
  * It never solves a CAPTCHA or answers an OTP/MFA prompt during a scheduled
    run.  Those states raise :class:`AuthenticationError` and stop the run.
  * It never suppresses or spoofs bot detection.  If Amazon serves a challenge,
    a human handles it via ``python run.py --login``.
"""

from __future__ import annotations

from time import monotonic
from urllib.parse import urljoin

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .auth import sign_in
from .browser import BrowserSession, safe_url
from .config import Settings
from .exceptions import (
    AuthenticationError,
    NavigationError,
    ReportBuildError,
    ReportNotFoundError,
    DownloadError,
)
from .logging_config import get_logger
from .selectors import find_first, is_present

logger = get_logger("amazon")


class ReportsPage:
    """High-level operations against the Ads console reporting pages."""

    def __init__(self, session: BrowserSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @property
    def page(self) -> Page:
        return self.session.page

    # -- navigation and sign-in ---------------------------------------------

    def open_builder(self) -> None:
        """Open the Reports list and click its Create report button.

        This is the path a person takes (``AMAZON_REPORTS_URL`` -> Create
        report).  If the button cannot be found, the Create report page is
        opened directly from ``AMAZON_NEW_REPORT_URL`` instead, with a warning,
        so a changed button does not stop the run.
        """
        self.open_list()
        button = find_first(self.page, "create_report_button", timeout_ms=10_000)
        if button is not None:
            logger.info("Clicking Create report")
            button.click()
            self.wait_for("report_builder_marker", "Create report")
            return
        self.session.capture_failure("create-report-button-missing")
        logger.warning(
            "No Create report button on the Reports list (config/selectors.json "
            "'create_report_button'); opening the Create report page directly"
        )
        self._open(self.settings.new_report_url_with_entity, "report_builder_marker", "Create report")

    def open_list(self) -> None:
        """Open the Reports list, signing in first if the session has lapsed."""
        self._open(self.settings.reports_url_with_entity, "reports_page_marker", "Reports list")

    def _open(self, url: str, marker: str, label: str) -> None:
        self.goto(url, label)
        if self.is_showing_sign_in():
            if not self.settings.can_auto_login:
                self.session.capture_failure("sign-in-required")
                raise AuthenticationError(
                    "Amazon session is missing or expired (sign-in page shown), and "
                    "automatic sign-in is not configured. Set AMAZON_EMAIL and "
                    "AMAZON_PASSWORD, or re-authenticate with: python run.py --login"
                )
            sign_in(self.session, self.settings)
            # Amazon usually redirects back, but not always to the same page.
            if not is_present(self.page, marker, timeout_ms=5_000):
                self.goto(url, label)
        self.assert_authenticated()
        self.wait_for(marker, label)

    def goto(self, url: str, label: str) -> None:
        logger.info("Navigating to %s (entity %s)", label, self.settings.entity_id)
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Timed out loading {label}: {exc}") from exc
        except Exception as exc:
            raise NavigationError(f"Could not load {label}: {exc}") from exc

    def is_showing_sign_in(self) -> bool:
        url = self.page.url.lower()
        if any(marker in url for marker in ("/ap/signin", "/ap/mfa", "signin")):
            return True
        return is_present(self.page, "sign_in_marker", timeout_ms=1_500)

    def assert_authenticated(self) -> None:
        """Stop the run if Amazon is showing sign-in or a security challenge.

        Raises:
            AuthenticationError: Non-transient on purpose: never retried, and
                no attempt is made to satisfy a challenge.
        """
        if is_present(self.page, "security_challenge_marker", timeout_ms=1_500):
            self.session.capture_failure("security-challenge")
            raise AuthenticationError(
                "Amazon presented a security challenge (CAPTCHA/OTP/MFA). "
                "The pipeline does not answer these. Complete it once by hand with "
                "'python run.py --login'; the session is then reused."
            )
        if self.is_showing_sign_in():
            self.session.capture_failure("sign-in-required")
            raise AuthenticationError(
                "Amazon is still showing the sign-in page. If credentials are set, "
                "they may be wrong or the account needs a challenge answered by a "
                "person: python run.py --login"
            )

    def wait_for(self, marker: str, label: str) -> None:
        found = find_first(self.page, marker, timeout_ms=self.settings.browser_timeout_ms)
        if found is None:
            self.session.capture_failure(f"{marker}-missing")
            raise NavigationError(
                f"{label} never appeared within {self.settings.browser_timeout_ms} ms at "
                f"{safe_url(self.page.url)}. Either it had not finished loading, or the "
                f"layout changed -- update config/selectors.json ({marker!r})."
            )
        self.wait_for_idle()
        logger.info("%s loaded", label)

    def wait_for_idle(self) -> None:
        """Wait for loading indicators to clear and the network to settle (bounded)."""
        budget_ms = min(self.settings.browser_timeout_ms, 15_000)
        indicator = find_first(self.page, "loading_indicator", timeout_ms=500)
        if indicator is not None:
            try:
                indicator.wait_for(state="hidden", timeout=budget_ms)
            except PlaywrightTimeoutError:
                logger.debug("Loading indicator still visible after %d ms", budget_ms)
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(budget_ms, 10_000))
        except PlaywrightTimeoutError:
            logger.debug("networkidle not reached; continuing")

    # -- step 1-3: build and run the report -----------------------------------

    def create_report(self, name: str) -> None:
        """Fill in Create report and click Run report.

        Must be called with the builder open (:meth:`open_builder`).  Returns
        as soon as Run report has been clicked: from that moment the report
        exists in Amazon, and the caller must not create it again.

        Raises:
            ReportBuildError: A form control could not be found or set.  Raised
                only *before* Run report is clicked, so nothing was created and
                a retry is safe.
        """
        logger.info("Creating report %r", name)
        self._ensure_dropdown(
            "Report category", "Sponsored Products",
            current="report_category_current", trigger="report_category_trigger",
            option="report_category_option",
        )
        self._ensure_dropdown(
            "Report type", "Purchased product",
            current="report_type_current", trigger="report_type_trigger",
            option="report_type_option",
        )
        self._check_radio("time_unit_daily", "Time unit: Daily")
        if self.settings.report_period:
            self._ensure_dropdown(
                "Report period", self.settings.report_period,
                current="report_period_current", trigger="report_period_trigger",
                option="report_period_option",
            )

        # Amazon rewrites the name whenever the report type changes, so the
        # name is filled in last.
        name_input = find_first(self.page, "report_name_input", timeout_ms=10_000)
        if name_input is None:
            self.session.capture_failure("report-name-input-missing")
            raise ReportBuildError(
                "Could not find the report Name field (config/selectors.json "
                "'report_name_input'). It is needed to find this run's report again."
            )
        name_input.fill(name)
        if name_input.input_value().strip() != name:
            self.session.capture_failure("report-name-not-set")
            raise ReportBuildError(f"Typed the report name but the field shows {name_input.input_value()!r}")

        run = find_first(self.page, "run_report_button", timeout_ms=10_000)
        if run is None:
            self.session.capture_failure("run-report-button-missing")
            raise ReportBuildError(
                "Could not find the Run report button (config/selectors.json 'run_report_button')."
            )
        run.click()
        logger.info("Clicked Run report for %r", name)

    def _ensure_dropdown(
        self, label: str, value: str, *, current: str, trigger: str, option: str
    ) -> None:
        """Make a dropdown show ``value``, touching it only if it does not already.

        The console's dropdowns are buttons that display the current value.
        Clicking one that is already right just opens its menu -- which then
        covers the rest of the form and blocks every later click.  So: check,
        open only if needed, pick, close, and verify.
        """
        params = {"value": value}
        if is_present(self.page, current, timeout_ms=3_000, params=params):
            logger.info("%s is already %s", label, value)
            return
        opener = find_first(self.page, trigger, timeout_ms=self.settings.browser_timeout_ms)
        if opener is None:
            self.session.capture_failure(f"{trigger}-missing")
            raise ReportBuildError(f"Could not find the {label} dropdown (config/selectors.json {trigger!r}).")
        opener.click()
        choice = find_first(self.page, option, timeout_ms=10_000, params=params)
        if choice is None:
            self.session.capture_failure(f"{option}-missing")
            self.page.keyboard.press("Escape")
            raise ReportBuildError(
                f"The {label} dropdown opened but offered no {value!r} (config/selectors.json {option!r})."
            )
        choice.click()
        if not is_present(self.page, current, timeout_ms=5_000, params=params):
            self.page.keyboard.press("Escape")
            self.session.capture_failure(f"{current}-not-confirmed")
            raise ReportBuildError(f"Chose {value!r} for {label}, but the dropdown does not show it.")
        logger.info("Selected %s: %s", label, value)

    def _check_radio(self, key: str, label: str) -> None:
        radio = find_first(self.page, key, timeout_ms=self.settings.browser_timeout_ms)
        if radio is None:
            self.session.capture_failure(f"{key}-missing")
            raise ReportBuildError(f"Could not find {label} (config/selectors.json {key!r}).")
        radio.check()
        logger.info("Selected %s", label)

    # -- step 4: wait until it can be downloaded -------------------------------

    def filter_to_report(self, name: str) -> None:
        """Type the report name into the list's search box, when there is one."""
        search = find_first(self.page, "report_search_input", timeout_ms=3_000)
        if search is None:
            return
        search.fill(name)
        search.press("Enter")
        self.wait_for_idle()

    def history_url(self, name: str) -> str | None:
        """URL of the report's own page (its runs), or None if not listed yet.

        The Reports list has no Download: its rows only link the report name to
        ``/reports/history/<id>``, where each run and its Download are listed.
        """
        self.open_list()
        self.filter_to_report(name)
        link = find_first(self.page, "report_link", timeout_ms=10_000, params={"report_name": name})
        if link is None:
            return None
        href = link.get_attribute("href") or ""
        return urljoin(self.page.url, href) if href else None

    def wait_until_downloadable(self, name: str) -> Locator:
        """Reload the report's page until one of its runs has a Download control.

        Polls every ``REPORT_POLL_SECONDS`` for up to ``REPORT_READY_TIMEOUT_MS``.
        Never clicks the page's own Run report button.

        Raises:
            ReportNotFoundError: The report never appeared in the list -- it was
                not created, or the list markup changed.  Not retried.
            DownloadError: It appeared but was still not ready when time ran
                out (transient: the retry loop polls again without
                re-creating it).
        """
        deadline = monotonic() + self.settings.report_ready_timeout_ms / 1000
        poll_ms = int(self.settings.report_poll_seconds * 1000)
        url: str | None = None
        attempt = 0

        while True:
            attempt += 1
            if url is None:
                url = self.history_url(name)
                if url is None:
                    logger.info("Report %r is not listed yet (check %d)", name, attempt)
            if url is not None:
                self._open(url, "report_history_marker", "Report history")
                control = find_first(self.page, "run_download_control", timeout_ms=3_000)
                if control is not None:
                    logger.info("Report %r is ready (check %d)", name, attempt)
                    return control
                logger.info(
                    "Report %r not ready yet (check %d, run status: %s); reloading",
                    name, attempt, self._run_statuses() or "unknown",
                )

            if monotonic() + poll_ms / 1000 >= deadline:
                break
            self.page.wait_for_timeout(poll_ms)

        if url is None:
            self.session.capture_failure("report-never-listed")
            raise ReportNotFoundError(
                f"Report {name!r} never appeared in the Reports list within "
                f"{self.settings.report_ready_timeout_ms // 1000}s of being created. Check "
                "the screenshot; if it is listed, update config/selectors.json ('report_link')."
            )
        self.session.capture_failure("report-not-ready")
        raise DownloadError(
            f"Report {name!r} was still not downloadable after "
            f"{self.settings.report_ready_timeout_ms // 1000}s; will keep waiting on retry. "
            "If a run shows as completed, update config/selectors.json ('run_download_control')."
        )

    def _run_statuses(self) -> str:
        """The Status cell of each run on the report's page, for the log."""
        try:
            cells = self.page.locator("[role=grid] [role=row]:has([role=gridcell])").locator(
                "[role=gridcell] >> nth=0"
            )
            return ", ".join(c.strip() for c in cells.all_inner_texts()[:5])
        except Exception:  # diagnostics only
            return ""

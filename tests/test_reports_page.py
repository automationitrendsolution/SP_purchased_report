"""Reports list -> Create report -> run -> history page -> Download, against a
mock console in real Chromium.

The mock follows the live Sponsored ads report center as captured on
2026-11-23 (see the _note_console entry in config/selectors.json):

* Report category / Report type / Report period are dropdown *buttons* showing
  the current value.  An open menu lays a page-wide layer over the form, so a
  needless click on a dropdown blocks every later click -- the 60 s hang seen
  on the first live run.
* The list has no Download; each report name links to /reports/history/<id>,
  whose runs grid has the Download, and which has its own Run report button
  that must never be clicked by the pipeline.

Playwright routes serve every page; anything else is aborted, so no request
leaves the machine.  Skipped where Chromium is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.amazon_ads import ReportsPage
from app.downloader import download_report
from app.exceptions import DownloadError, ReportBuildError, ReportNotFoundError

CSV_BODY = "Date%2CCountry%2CAdvertised%20ASIN%2CPurchased%20ASIN%0A2026-11-01%2CUS%2CB1%2CB2"

BUILDER = """
<style>
  #shade { position: fixed; inset: 0; z-index: 10; background: rgba(0,0,0,.01); }
  [role=listbox] { position: relative; z-index: 20; background: white; }
</style>
<div id=shade hidden onclick="closeMenus()"></div>
<p>Configuration</p>
<span>Report category</span>
<button>Report availability varies by category.</button>
<button id=cat onclick="openMenu('catmenu')">Sponsored Products</button>
<ul id=catmenu role=listbox hidden>
  <li role=option onclick="pick('cat', 'Sponsored Products')">Sponsored Products</li>
  <li role=option onclick="pick('cat', 'Sponsored Brands')">Sponsored Brands</li>
</ul>
<span>Report type</span>
<button id=rt onclick="openMenu('menu')">Search term</button>
<ul id=menu role=listbox hidden>
  <li role=option onclick="pick('rt', 'Search term')">Search term</li>
  <li role=option onclick="pick('rt', 'Purchased product')">Purchased product</li>
</ul>
<span>Time unit</span>
<div role=group>
  <input type=radio name=unit id=u1 checked><label for=u1>Summary</label>
  <input type=radio name=unit id=u2><label for=u2>Daily</label>
</div>
<span>Report period</span>
<button id=period aria-label="Report period" onclick="openMenu('pmenu')"><p>Last 30 days</p></button>
<ul id=pmenu role=listbox hidden>
  <li role=option onclick="pick('period', 'Last 7 days')">Last 7 days</li>
  <li role=option onclick="pick('period', 'Last 30 days')">Last 30 days</li>
</ul>
<input id=name aria-label="Enter a report name" value="Sponsored Products Search term report">
<button onclick="run()">Run report</button>
<script>
  function openMenu(id) { document.getElementById(id).hidden = false;
                          document.getElementById('shade').hidden = false; }
  function closeMenus() { document.querySelectorAll('[role=listbox]').forEach(m => m.hidden = true);
                          document.getElementById('shade').hidden = true; }
  function pick(id, value) {
    const el = document.getElementById(id);
    if (id === 'period') el.innerHTML = '<p>' + value + '</p>'; else el.textContent = value;
    if (id === 'rt') document.getElementById('name').value = 'Sponsored Products ' + value + ' report';
    closeMenus();
  }
  function run() {
    const q = new URLSearchParams({
      created: document.getElementById('name').value,
      cat: document.getElementById('cat').textContent,
      type: document.getElementById('rt').textContent,
      unit: document.getElementById('u2').checked ? 'Daily' : 'Summary',
      period: document.getElementById('period').textContent,
    });
    location.href = '/reports/submit?' + q;
  }
</script>
"""


def list_page(reports: list[tuple[str, str]], *, create_button: bool) -> str:
    rows = "".join(
        f"<div role=row><div role=gridcell><p>No reports scheduled</p></div>"
        f"<div role=gridcell><a href='/reports/history/{rid}?entityId=ENTITYTEST'>{name}</a></div></div>"
        for name, rid in reports
    )
    if create_button:
        button = (
            "<button onclick=\"location.href='/reports/new?entityId=ENTITYTEST'\">Create report</button>"
        )
    else:
        button = "<h1>Reports</h1>"
    return (
        f"{button}<input type=search placeholder='Search'>"
        f"<div role=grid><div role=rowgroup>{rows}</div></div>"
    )


def history_page(name: str, ready: bool) -> str:
    if ready:
        run = (
            "<div role=row><div role=gridcell><p>Completed</p></div>"
            "<div role=gridcell><p>Last 30 days</p></div><div role=gridcell>"
            f"<a href='data:text/csv;charset=utf-8,{CSV_BODY}' download='report.csv'>Download</a>"
            "</div></div>"
        )
    else:
        run = "<div role=row><div role=gridcell><p>Pending</p></div><div role=gridcell><p>—</p></div></div>"
    return (
        f"<h1>Report:{name}</h1><button onclick=\"location.href='/reports/rerun'\">Run report</button>"
        "<div role=grid><div role=rowgroup><div role=row><div role=columnheader>Status</div></div></div>"
        f"<div role=rowgroup>{run}</div></div>"
    )


@dataclass
class MockConsole:
    """State behind the routed pages."""

    ready_after: int = 2  # history loads that show Pending before Download
    created: dict[str, str] | None = None
    submissions: int = 0
    reruns: int = 0  # clicks on the history page's own Run report -- must stay 0
    list_loads: int = 0
    builder_loads: int = 0
    history_loads: int = 0
    create_button: bool = True
    other_reports: list[tuple[str, str]] = field(
        default_factory=lambda: [("Sponsored Products Purchased product report", "old")]
    )

    def handle(self, route) -> None:
        url = urlsplit(route.request.url)
        html = None
        if url.path == "/reports/new":
            self.builder_loads += 1
            html = BUILDER
        elif url.path == "/reports/submit":
            self.submissions += 1
            self.created = {k: v[0] for k, v in parse_qs(url.query).items()}
            # Not an HTTP redirect: Playwright does not route redirects, so a 302
            # here would send the follow-up request to the real site.
            html = "<script>location.replace('/reports?entityId=ENTITYTEST')</script>"
        elif url.path == "/reports/rerun":
            self.reruns += 1
            html = "<p>rerun</p>"
        elif url.path == "/reports":
            self.list_loads += 1
            reports = list(self.other_reports)
            if self.created:
                reports.insert(0, (self.created["created"], "new"))
            html = list_page(reports, create_button=self.create_button)
        elif url.path == "/reports/history/new":
            self.history_loads += 1
            html = history_page(self.created["created"], self.history_loads > self.ready_after)
        elif url.path == "/reports/history/old":
            html = history_page("old", True)
        if html is None:
            return route.abort()
        return route.fulfill(content_type="text/html", body=html)


class FakeSession:
    def __init__(self, page) -> None:
        self.page = page
        self.failures: list[str] = []

    def capture_failure(self, label: str) -> dict[str, str]:
        self.failures.append(label)
        return {}


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as p:
            instance = p.chromium.launch()
            yield instance
            instance.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-specific
        pytest.skip(f"Chromium not available: {exc}")


@pytest.fixture
def console(browser, settings):
    mock = MockConsole()
    context = browser.new_context(accept_downloads=True)
    # Offline guarantee: anything not served by the mock is aborted.  Later
    # routes take precedence, so the catch-all is registered first.
    context.route("**/*", lambda route: route.abort())
    context.route("https://advertising.amazon.com/**", mock.handle)
    page = context.new_page()
    page.set_default_timeout(5_000)
    for key, value in (
        ("browser_timeout_ms", 3_000),
        ("download_timeout_ms", 5_000),
        ("report_poll_seconds", 0.2),
        ("report_ready_timeout_ms", 30_000),
    ):
        object.__setattr__(settings, key, value)
    session = FakeSession(page)
    yield mock, session, ReportsPage(session, settings)
    context.close()


def create(reports: ReportsPage, name: str) -> None:
    reports.open_builder()
    reports.create_report(name)
    reports.page.wait_for_url("**/reports?**")


def test_creates_the_report_with_the_right_choices(console) -> None:
    mock, _session, reports = console
    create(reports, "SP Purchased product auto 20261123T183000Z")
    assert mock.created == {
        "created": "SP Purchased product auto 20261123T183000Z",
        "cat": "Sponsored Products",
        "type": "Purchased product",
        "unit": "Daily",
        "period": "Last 30 days",
    }


def test_a_dropdown_already_right_is_not_clicked(console) -> None:
    """Clicking 'Sponsored Products' when it is already selected opens a menu
    whose page-wide layer blocks every later click (the live 60 s hang)."""
    mock, _session, reports = console
    create(reports, "SP check")  # would time out on the next click if the menu were opened
    assert mock.created["cat"] == "Sponsored Products"


def test_report_period_is_chosen_when_configured(console) -> None:
    mock, _session, reports = console
    object.__setattr__(reports.settings, "report_period", "Last 7 days")
    create(reports, "SP period")
    assert mock.created["period"] == "Last 7 days"


def test_unknown_period_fails_before_run_report(console) -> None:
    mock, _session, reports = console
    object.__setattr__(reports.settings, "report_period", "Last 999 days")
    reports.open_builder()
    with pytest.raises(ReportBuildError) as caught:
        reports.create_report("SP bad period")
    assert caught.value.transient
    assert mock.submissions == 0  # nothing was created


def test_waits_on_the_history_page_and_downloads_this_report(console, settings) -> None:
    mock, session, reports = console
    name = "SP Purchased product auto 20261123T183000Z"
    create(reports, name)

    result = download_report(session, reports, settings, name)

    assert result.suggested_filename == "report.csv"
    assert mock.history_loads == mock.ready_after + 1  # reloaded until Download appeared
    assert mock.submissions == 1
    assert mock.reruns == 0  # the history page's Run report was never clicked


def test_starts_from_the_reports_list_and_clicks_create_report(console) -> None:
    mock, session, reports = console
    reports.open_builder()
    assert (mock.list_loads, mock.builder_loads) == (1, 1)
    assert "create-report-button-missing" not in session.failures


def test_falls_back_to_the_create_report_url_without_the_button(console) -> None:
    mock, session, reports = console
    mock.create_button = False
    reports.open_builder()
    assert mock.builder_loads == 1
    assert "create-report-button-missing" in session.failures


def test_not_ready_in_time_is_transient(console) -> None:
    mock, _session, reports = console
    mock.ready_after = 10_000
    object.__setattr__(reports.settings, "report_ready_timeout_ms", 3_000)
    create(reports, "SP slow")
    with pytest.raises(DownloadError) as caught:
        reports.wait_until_downloadable("SP slow")
    assert caught.value.transient


def test_report_that_never_appears_is_not_retried(console) -> None:
    _mock, session, reports = console
    object.__setattr__(reports.settings, "report_ready_timeout_ms", 3_000)
    with pytest.raises(ReportNotFoundError) as caught:
        reports.wait_until_downloadable("SP never created")
    assert not caught.value.transient
    assert "report-never-listed" in session.failures


def test_pipeline_retry_does_not_create_a_second_report(console, settings, monkeypatch) -> None:
    """A failure after Run report must lead back to waiting, not to re-creating."""
    from app import main as main_module
    from app.main import ReportPipeline, RunMode, RunOptions

    mock, session, _reports = console
    object.__setattr__(settings, "max_retries", 2)

    class SessionCM:
        def __init__(self, *_a, **_k) -> None:
            pass

        def __enter__(self):
            return session

        def __exit__(self, *_exc) -> None:
            return None

    calls = {"n": 0}
    real = main_module.download_report

    def flaky_download(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DownloadError("simulated: browser crashed while waiting")
        return real(*args, **kwargs)

    monkeypatch.setattr(main_module, "BrowserSession", SessionCM)
    monkeypatch.setattr(main_module, "download_report", flaky_download)

    pipeline = ReportPipeline(settings, RunOptions(mode=RunMode.DOWNLOAD_ONLY))
    pipeline.run()

    assert mock.submissions == 1
    assert mock.reruns == 0
    assert calls["n"] == 2
    assert pipeline.summary.report_name.startswith(settings.report_name_prefix)

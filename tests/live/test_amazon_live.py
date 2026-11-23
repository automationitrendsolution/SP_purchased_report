"""Read-only check against the real Ads console.  Needs a saved session.

Opens Create report and the Reports list.  It does NOT click Run report (that
would create a report in the account) -- use
`uv run python run.py --download-only --headed` for the full flow.
"""

from __future__ import annotations

import pytest

from app.amazon_ads import ReportsPage
from app.browser import BrowserSession
from app.config import load_settings
from app.selectors import find_first

pytestmark = pytest.mark.live


def test_create_report_page_and_list_load() -> None:
    settings = load_settings()
    with BrowserSession(settings) as session:
        reports = ReportsPage(session, settings)
        reports.open_builder()
        for key in ("report_category_trigger", "report_type_trigger", "run_report_button"):
            assert find_first(session.page, key, timeout_ms=10_000) is not None, key
        reports.open_list()

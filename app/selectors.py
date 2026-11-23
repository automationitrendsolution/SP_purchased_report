"""Selector catalogue and resolution helpers.

Amazon ships DOM changes to the Ads console without notice, so every element is
addressed by an ordered list of *candidate* selectors loaded from
``config/selectors.json``.  Fixing a broken run is then a config edit, not a
code change and redeploy.

Candidates may contain ``{report_name}``; it is filled in (quote-escaped) from
the ``params`` passed to :func:`find_first`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from time import monotonic, sleep

from playwright.sync_api import Locator, Page

from .config import PROJECT_ROOT
from .exceptions import ConfigurationError
from .logging_config import get_logger

logger = get_logger("selectors")

DEFAULT_SELECTOR_FILE = PROJECT_ROOT / "config" / "selectors.json"

#: Default budget for one search.
CANDIDATE_TIMEOUT_MS = 2_500

#: Pause between passes over the candidate list.
POLL_INTERVAL_MS = 250


def _pause(scope: Page | Locator, seconds: float) -> None:
    """Wait without blocking Playwright's event loop (downloads, dialogs)."""
    page = scope if isinstance(scope, Page) else getattr(scope, "page", None)
    if page is not None:
        page.wait_for_timeout(seconds * 1000)
    else:  # pragma: no cover - defensive
        sleep(seconds)


@lru_cache(maxsize=4)
def load_selectors(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Load the selector catalogue, ignoring ``_``-prefixed comment keys."""
    source = path or DEFAULT_SELECTOR_FILE
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Selector file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Selector file {source} is not valid JSON: {exc}") from exc

    catalogue: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigurationError(
                f"Selector entry {key!r} in {source} must be a list of selector strings"
            )
        catalogue[key] = tuple(value)
    return catalogue


def candidates(
    name: str, path: Path | None = None, params: dict[str, str] | None = None
) -> tuple[str, ...]:
    """Selector candidates registered under ``name``, with ``params`` filled in."""
    catalogue = load_selectors(path)
    try:
        raw = catalogue[name]
    except KeyError as exc:
        raise ConfigurationError(f"No selector candidates configured for {name!r}") from exc
    if not params:
        return raw
    escaped = {k: _escape(v) for k, v in params.items()}
    return tuple(_fill(selector, escaped) for selector in raw)


def _escape(value: str) -> str:
    """Make a value safe inside a single- or double-quoted selector string."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')


def _fill(selector: str, params: dict[str, str]) -> str:
    # str.format would choke on the braces inside CSS / regex selectors, so only
    # the known placeholders are replaced.
    for key, value in params.items():
        selector = selector.replace("{" + key + "}", value)
    return selector


def find_first(
    scope: Page | Locator,
    name: str,
    *,
    timeout_ms: int = CANDIDATE_TIMEOUT_MS,
    path: Path | None = None,
    params: dict[str, str] | None = None,
) -> Locator | None:
    """Return the first candidate for ``name`` that is visible, else None.

    ``scope`` may be the page or a locator (e.g. one report row), in which case
    the search is confined to it.  ``timeout_ms`` is the budget for the **whole
    search**.

    Every pass checks **all** candidates instantly (no per-candidate wait), in
    list order, and passes repeat every ``POLL_INTERVAL_MS`` until one is
    visible or the budget runs out.  So the earliest-listed visible candidate
    wins, every candidate is tried even under a short budget, and a missing
    candidate costs nothing.  Only *visible* matches count: a hidden copy of an
    element (a closed menu, an off-screen duplicate) never shadows a visible one.
    """
    deadline = monotonic() + timeout_ms / 1000
    selectors = candidates(name, path, params)
    broken: set[str] = set()

    while True:
        for selector in selectors:
            if selector in broken:
                continue
            try:
                # Inside the try on purpose: a malformed selector raises here,
                # and one bad entry must not take the whole list down with it.
                locator = scope.locator(selector).filter(visible=True).first
                if locator.count():
                    logger.debug("Matched %s via %r", name, selector)
                    return locator
            except Exception as exc:
                logger.debug("Selector %r for %s raised %s", selector, name, exc)
                broken.add(selector)

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        _pause(scope, min(POLL_INTERVAL_MS / 1000, remaining))

    logger.debug(
        "No candidate for %s became visible within %d ms (tried %d selector(s))",
        name,
        timeout_ms,
        len(selectors),
    )
    return None


def is_present(
    scope: Page | Locator,
    name: str,
    *,
    timeout_ms: int = 1_000,
    path: Path | None = None,
    params: dict[str, str] | None = None,
) -> bool:
    """Whether any candidate for ``name`` is currently visible."""
    return find_first(scope, name, timeout_ms=timeout_ms, path=path, params=params) is not None

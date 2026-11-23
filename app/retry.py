"""Retry with exponential backoff, restricted to transient failures.

Authentication, CAPTCHA, MFA and configuration errors are deliberately excluded:
hammering a security challenge is both futile and hostile to the service.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from playwright.sync_api import Error as PlaywrightError

from .exceptions import ReportFlowError
from .logging_config import get_logger

T = TypeVar("T")
logger = get_logger("retry")


def is_transient(error: BaseException) -> bool:
    """Whether ``error`` is worth retrying."""
    if isinstance(error, ReportFlowError):
        return error.transient
    # Each attempt starts a fresh Chromium, so any Playwright failure (timeout,
    # crashed page) is worth another try.
    if isinstance(error, PlaywrightError):
        return True
    return isinstance(error, (ConnectionError, TimeoutError, OSError))


def compute_delay(attempt: int, base: float, maximum: float) -> float:
    """Exponential backoff with full jitter, capped at ``maximum``."""
    if base <= 0:
        return 0.0
    exponential = min(base * (2 ** (attempt - 1)), maximum if maximum > 0 else base)
    return random.uniform(0, exponential)


def retry_call(
    operation: Callable[[], T],
    *,
    description: str,
    attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying transient failures up to ``attempts`` times.

    Raises:
        The last exception raised by ``operation`` once attempts are exhausted,
        or immediately if the failure is not transient.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if not is_transient(exc):
                logger.error(
                    "%s failed with a non-retryable error: %s: %s",
                    description,
                    type(exc).__name__,
                    exc,
                )
                raise
            if attempt >= attempts:
                logger.error(
                    "%s failed after %d attempt(s): %s: %s",
                    description,
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                raise
            delay = compute_delay(attempt, base_delay, max_delay)
            logger.warning(
                "%s failed on attempt %d/%d (%s: %s); retrying in %.1fs",
                description,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            if delay:
                sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover

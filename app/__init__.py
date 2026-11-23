"""Sponsored Products 'Purchased product' report automation.

Playwright signs in to the Amazon Ads console, creates a Sponsored Products
"Purchased product" (Daily) report, waits for it, downloads it, and loads it
into MySQL with pandas.  No Amazon Ads API.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]

"""Scraper error types."""

from __future__ import annotations


class SessionExpiredError(Exception):
    pass


class ScraperError(Exception):
    pass

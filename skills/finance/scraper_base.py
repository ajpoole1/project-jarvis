"""Playwright session lifecycle for bank scrapers.

Session storage lives at /data/browser-profiles/{bank}_state.json.
First-auth: headful browser, wait for user to complete MFA, then save and exit.
Subsequent runs: headless, restore session from storage state.

Playwright is imported lazily inside each async function so this module can be
imported (and tested) in environments where playwright is not installed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

_PROFILES_DIR = Path("/data/browser-profiles")


def _state_path(bank: str) -> Path:
    return _PROFILES_DIR / f"{bank}_state.json"


async def ensure_session(bank: str, force_headful: bool = False) -> tuple[object, BrowserContext]:
    """Return (playwright_instance, BrowserContext) for the given bank.

    If no saved state exists or force_headful is True, launches a headful browser,
    waits for the user to complete login + MFA, saves the session, then returns.
    Caller is responsible for closing playwright_instance when done.
    """
    from playwright.async_api import async_playwright

    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    state_file = _state_path(bank)

    pw = await async_playwright().start()
    try:
        if not state_file.exists() or force_headful:
            print(
                f"[finance scraper] No saved session for {bank}. "
                "Opening browser for first-time login + MFA."
            )
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            print(
                f"[finance scraper] Please log in to {bank} and complete MFA, "
                "then press ENTER here."
            )
            await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            await save_session(bank, context)
            return pw, context
        else:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(state_file))
            return pw, context
    except Exception:
        await pw.stop()
        raise


async def save_session(bank: str, context: BrowserContext) -> None:
    """Persist BrowserContext storage state to /data/browser-profiles/{bank}_state.json."""
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    state_file = _state_path(bank)
    await context.storage_state(path=str(state_file))


async def is_auth_expired(page: Page) -> bool:
    """Return True if the current page URL indicates a login redirect."""
    url = page.url.lower()
    return "signin" in url or "login" in url

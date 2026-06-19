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

_PROFILES_DIR = Path.home() / ".jarvis" / "browser-profiles"


def _state_path(bank: str) -> Path:
    return _PROFILES_DIR / f"{bank}_state.json"


async def ensure_session(
    bank: str, force_headful: bool = False, login_url: str | None = None
) -> tuple[object, BrowserContext]:
    """Return (playwright_instance, BrowserContext) for the given bank.

    If no saved state exists or force_headful is True, launches a headful browser,
    navigates to login_url (if provided), waits for the user to complete login + MFA,
    saves the session, then returns.
    Caller is responsible for closing playwright_instance when done.
    """
    from playwright.async_api import async_playwright

    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    state_file = _state_path(bank)

    pw = await async_playwright().start()
    try:
        if not state_file.exists() or force_headful:
            print(
                f"[finance scraper] First-auth mode for {bank}.\n"
                "  1. Close Chrome completely.\n"
                '  2. Launch Chrome with: chrome.exe --remote-debugging-port=9222\n'
                "     (see README for the exact command)\n"
                f"  3. Navigate to: {login_url or 'your bank login page'}\n"
                "  4. Log in and complete MFA.\n"
                "  5. Press ENTER here when done."
            )
            try:
                with open("/dev/tty") as tty:
                    await asyncio.get_event_loop().run_in_executor(None, tty.readline)
            except OSError:
                await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

            # Connect to the already-running Chrome via CDP over TCP.
            # Chrome was launched by the user with --remote-debugging-port=9222.
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            await save_session(bank, context)
            await browser.close()
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

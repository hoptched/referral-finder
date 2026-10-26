"""LinkedIn browser session helpers built on linkedin_scraper."""

from contextlib import asynccontextmanager
from pathlib import Path

import linkedin_scraper.core.auth as _auth
import linkedin_scraper.scrapers.base as _base
from linkedin_scraper import BrowserManager, RateLimitError, is_logged_in, wait_for_manual_login
from playwright.async_api import Page

LOGIN_TIMEOUT_MS = 300_000

RATE_LIMIT_PHRASES = ("too many requests", "rate limit", "slow down", "try again later")
# LinkedIn's limit/error pages are short. Real profiles and search results are long, and
# can contain the phrases as ordinary content ("Implemented Rate Limiting across ..."),
# so phrases only count on pages shorter than this many visible characters.
ERROR_PAGE_MAX_CHARS = 1500
BLOCKED_STATUSES = {429, 999}  # 999 = LinkedIn's bot block


async def detect_rate_limit(page: Page, status: int | None = None) -> None:
    """Replacement for linkedin_scraper's detect_rate_limit.

    Upstream matches phrases against body.text_content(), which includes
    inline <script> JSON. LinkedIn profiles embed a hidden "Something went
    wrong. Please try again later." string there, so every profile scrape
    falsely raised. This version checks the HTTP status, checkpoint/CAPTCHA,
    and the phrases only in the visible text of short (error-sized) pages.
    """
    if status in BLOCKED_STATUSES:
        raise RateLimitError(f"LinkedIn refused the page (HTTP {status}).", suggested_wait_time=3600)
    if "linkedin.com/checkpoint" in page.url or "authwall" in page.url:
        raise RateLimitError("LinkedIn security checkpoint detected.", suggested_wait_time=3600)
    if await page.locator('iframe[title*="captcha" i], iframe[src*="captcha" i]').count():
        raise RateLimitError("CAPTCHA detected.", suggested_wait_time=3600)
    try:
        visible = (await page.locator("body").inner_text(timeout=1000)).lower()
    except Exception:
        return
    if len(visible) < ERROR_PAGE_MAX_CHARS and any(p in visible for p in RATE_LIMIT_PHRASES):
        raise RateLimitError("Rate limit message detected on page.", suggested_wait_time=1800)


# Both modules imported the upstream function by name, so patch each binding.
_base.detect_rate_limit = detect_rate_limit
_auth.detect_rate_limit = detect_rate_limit


async def create_session(session_path: Path) -> None:
    """Open a visible browser, wait for the user to log in, save cookies."""
    async with BrowserManager(headless=False) as browser:
        await browser.page.goto("https://www.linkedin.com/login")
        await wait_for_manual_login(browser.page, timeout=LOGIN_TIMEOUT_MS)
        await browser.save_session(str(session_path))


@asynccontextmanager
async def open_session(session_path: Path, headless: bool = True):
    """Yield a BrowserManager with the saved session loaded.

    Raises FileNotFoundError if no session exists (run `refer login`).
    """
    if not session_path.exists():
        raise FileNotFoundError(f"No session at {session_path}. Run `refer login` first.")
    async with BrowserManager(headless=headless) as browser:
        await browser.load_session(str(session_path))
        yield browser


async def check_session(session_path: Path, headless: bool = True) -> bool:
    """Return True if the saved session is still logged in."""
    async with open_session(session_path, headless) as browser:
        await browser.page.goto("https://www.linkedin.com/feed/")
        return await is_logged_in(browser.page)

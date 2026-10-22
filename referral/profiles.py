"""Scrape a LinkedIn profile's experience and education.

linkedin_scraper's PersonScraper no longer matches LinkedIn's DOM (no <h1>,
obfuscated class names), so it returns empty profiles. Instead we take the
visible text of the details/experience and details/education pages and let
Claude structure it. This survives markup changes and loads only 2 pages per
person (PersonScraper loads ~7).
"""

import asyncio
import random
import re
from typing import Optional

from playwright.async_api import Page
from pydantic import BaseModel

from referral.linkedin import detect_rate_limit
from referral.llm import client, log_usage

# Text after these markers is sidebar/footer, not profile content.
FOOTER_MARKERS = ("Profile language", "Who your viewers also viewed", "People also viewed")

EXTRACT_PROMPT = """Below is the visible text of a LinkedIn profile's Experience and Education pages.
Extract every entry. Use null for anything not stated; don't guess.

Experience entries come in two layouts:
- Single role: title, then "Company · Employment type", then dates, then location.
- Several roles at one company: the company name comes first, then a total
  duration (e.g. "Full-time · 4 yrs 8 mos"), then each role's title and dates.
  Every role in the group gets that company. Team or product names shown under
  a role (e.g. "Premium", "Ads") are not the company; put them in description.
is_current is true when the end date is "Present".

{text}"""


class Experience(BaseModel):
    title: str
    company: str
    employment_type: Optional[str]
    start: Optional[str]
    end: Optional[str]
    is_current: bool
    location: Optional[str]
    description: Optional[str]


class Education(BaseModel):
    school: str
    degree: Optional[str]
    field_of_study: Optional[str]
    start_year: Optional[int]
    end_year: Optional[int]


class ProfileData(BaseModel):
    experiences: list[Experience]
    educations: list[Education]


class Profile(ProfileData):
    linkedin_url: str
    name: Optional[str]
    raw_text: Optional[str] = None  # what was sent to the model; kept for debugging


def _trim(text: str) -> str:
    for marker in FOOTER_MARKERS:
        if (i := text.find(marker)) != -1:
            text = text[:i]
    return text.strip()


async def _section_text(page: Page, url: str) -> str:
    await page.goto(url, wait_until="domcontentloaded")
    await detect_rate_limit(page)
    await page.locator("main").wait_for(timeout=15_000)
    await page.wait_for_timeout(2_000)  # sections hydrate after DOMContentLoaded
    return _trim(await page.locator("main").inner_text())


async def scrape_profile(
    page: Page,
    linkedin_url: str,
    model: str,
    delay_seconds: tuple[float, float] = (3, 6),
) -> Profile:
    base = linkedin_url.split("?")[0].rstrip("/")
    experience = await _section_text(page, f"{base}/details/experience/")
    # Resolve redirects (e.g. /in/me) to the canonical profile URL.
    base = page.url.split("/details/")[0]
    # Title is "<Name> | LinkedIn", prefixed "(3) " when there are notifications.
    title = re.sub(r"^\(\d+\)\s*", "", await page.title())
    name = title.rsplit("|", 1)[0].strip() if "|" in title else None

    await asyncio.sleep(random.uniform(*delay_seconds))
    education = await _section_text(page, f"{base}/details/education/")

    text = f"{experience}\n\n{education}"
    data = await extract_profile(text, model)
    return Profile(linkedin_url=base, name=name, raw_text=text, **data.model_dump())


async def extract_profile(text: str, model: str) -> ProfileData:
    """Structure the visible text of a profile's details pages."""
    response = await client().messages.parse(
        model=model,
        max_tokens=16000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
        output_format=ProfileData,
    )
    log_usage("profile", response)
    return response.parsed_output

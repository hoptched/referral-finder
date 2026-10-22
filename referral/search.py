"""Find current employees of a company who went to a given school.

linkedin_scraper has no people search, so this drives LinkedIn's own search
UI. Company IDs come from the company page's "see all employees" link (which
includes affiliated pages, e.g. LinkedIn = 1337 + 39939 + 2587638) and are
cached in .cache/companies.json.
"""

import asyncio
import json
import random
import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.async_api import Page
from pydantic import BaseModel

from referral.config import project_path
from referral.linkedin import detect_rate_limit

CACHE_PATH = project_path(".cache/companies.json")
DEGREE_RE = re.compile(r"•\s*(1st|2nd|3rd\+?)")

# Runs in the page. Each result card has exactly one "• 2nd"-style degree
# marker, so climb from each profile's name link until the parent would hold
# a second marker. (Mutual-connection names are also /in/ links, so "contains
# another profile link" is not a usable boundary, and those links must be
# filtered out separately.)
CARDS_JS = r"""() => {
  const main = document.querySelector('main');
  const degree = /•\s*(1st|2nd|3rd\+?)/g;
  const count = el => (el.innerText.match(degree) || []).length;
  const seen = new Set(), cards = [];
  for (const a of main.querySelectorAll('a[href*="/in/"]')) {
    // Some name links wrap the whole card; the name is their first line.
    const name = a.innerText.trim().split('\n')[0].replace(/\s*•.*$/, '').trim();
    if (!name) continue;
    const href = new URL(a.getAttribute('href'), 'https://www.linkedin.com');
    const slug = href.pathname.split('/')[2];
    if (!slug || seen.has(slug)) continue;
    let card = a;
    while (card.parentElement && card.parentElement !== main && count(card.parentElement) <= 1) {
      card = card.parentElement;
    }
    // A mutual-connection link climbs into someone else's card; a result's
    // own name link is always the card's first line.
    if (count(card) !== 1 || !card.innerText.trim().startsWith(name)) continue;
    seen.add(slug);
    cards.push({slug, name, text: card.innerText});
  }
  return cards;
}"""

ACTION_LINES = {
    "Connect", "Message", "Follow", "Following", "Pending",
    "View my portfolio", "Visit my website", "View my services", "Book an appointment", "View my newsletter",
}
FOLLOWERS_RE = re.compile(r"[\d,.]+K?\s+followers?")


class SearchResult(BaseModel):
    name: str
    linkedin_url: str
    degree: Optional[str]
    headline: Optional[str]
    location: Optional[str]
    summary: Optional[str]  # LinkedIn's one-line AI summary of the person


class Company(BaseModel):
    name: str
    linkedin_url: str
    ids: list[str]


def _compact(values: list[str]) -> str:
    # LinkedIn ignores the whole filter if the list has spaces (json.dumps default).
    return json.dumps(values, separators=(",", ":"))


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except FileNotFoundError:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


async def _goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")
    await detect_rate_limit(page)
    await page.locator("main").wait_for(timeout=15_000)
    await page.wait_for_timeout(3_000)  # results hydrate after DOMContentLoaded


# Runs in the page: one entry per company result, with the card's text (for follower counts).
COMPANY_CARDS_JS = r"""() => {
  const main = document.querySelector('main');
  const slugOf = a => (a.getAttribute('href').match(/\/company\/([^/?#]+)/) || [])[1];
  const slugs = el => new Set([...el.querySelectorAll('a[href*="/company/"]')].map(slugOf).filter(Boolean));
  const seen = new Set(), cards = [];
  for (const a of main.querySelectorAll('a[href*="/company/"]')) {
    const slug = slugOf(a), name = a.innerText.trim().split('\n')[0].trim();
    if (!slug || !name || seen.has(slug)) continue;
    let card = a;
    while (card.parentElement && card.parentElement !== main && slugs(card.parentElement).size <= 1) card = card.parentElement;
    seen.add(slug);
    cards.push({slug, name, text: card.innerText});
  }
  return cards;
}"""
GENERIC_WORDS_RE = re.compile(r"\b(group|holdings?|technologies|technology|labs|company|international|global|corporation)\b", re.I)
FOLLOWERS_COUNT_RE = re.compile(r"([\d,.]+)\s*([KM]?)\s+followers", re.I)


def _followers(text: str) -> int:
    m = FOLLOWERS_COUNT_RE.search(text)
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1_000, "M": 1_000_000}[m.group(2).upper()])


def company_queries(name: str) -> list[str]:
    """'Zillow Group' -> ['Zillow Group', 'Zillow']: LinkedIn's company search is phrase-like."""
    short = re.sub(r"\s+", " ", GENERIC_WORDS_RE.sub("", name)).strip(" ,&-")
    return [name] + ([short] if short and short.lower() != name.lower() else [])


def pick_company(cards: list[dict], names: list[str]) -> Optional[dict]:
    """Exact name match first, otherwise the most-followed result (the main page, not a subsidiary)."""
    wanted = {n.lower() for n in names}
    exact = [c for c in cards if c["name"].lower() in wanted]
    pool = exact or cards
    return max(pool, key=lambda c: _followers(c["text"]), default=None)


async def _find_company_url(page: Page, name: str) -> str:
    queries = company_queries(name)
    all_cards: list[dict] = []
    for i, query in enumerate(queries):
        if i:
            await asyncio.sleep(random.uniform(3, 6))
        await _goto(page, "https://www.linkedin.com/search/results/companies/?" + urlencode({"keywords": query}))
        cards = await page.evaluate(COMPANY_CARDS_JS)
        all_cards += cards
        exact = [c for c in cards if c["name"].lower() in {q.lower() for q in queries}]
        if exact:
            return f"https://www.linkedin.com/company/{pick_company(exact, queries)['slug']}/"
    best = pick_company(all_cards, queries)
    if best is None:
        raise LookupError(f"No LinkedIn company found for {name!r}")
    return f"https://www.linkedin.com/company/{best['slug']}/"


async def _company_ids(page: Page, company_url: str) -> list[str]:
    await _goto(page, company_url)
    hrefs = await page.locator('a[href*="search/results/people/"]').evaluate_all(
        "els => els.map(a => a.getAttribute('href'))"
    )
    for href in hrefs:
        query = parse_qs(urlsplit(urljoin("https://www.linkedin.com", href)).query)
        # The "see all employees" link filters on currentCompany alone.
        if "currentCompany" in query and not ({"schoolFilter", "pastCompany", "network"} & query.keys()):
            return [str(x) for x in json.loads(query["currentCompany"][0])]
    raise LookupError(f"No employee search link on {company_url}")


async def resolve_company(page: Page, name: str, company_url: Optional[str] = None) -> Company:
    if not name.strip() and not company_url:
        raise ValueError("No company name to look up on LinkedIn")
    cache = _load_cache()
    key = (company_url or name).lower()
    if key in cache:
        return Company(**cache[key])
    if company_url is None:
        company_url = await _find_company_url(page, name)
        await asyncio.sleep(random.uniform(3, 6))
    company = Company(name=name, linkedin_url=company_url, ids=await _company_ids(page, company_url))
    cache[key] = company.model_dump()
    # A URL given by the user is a correction: remember it for this company name too.
    cache[name.lower()] = company.model_dump()
    _save_cache(cache)
    return company


def _parse_card(slug: str, name: str, text: str) -> SearchResult:
    lines = [l.strip() for l in text.split("\n") if l.strip() and l.strip() != "•"]
    degree = next((m.group(1) for l in lines[:3] if (m := DEGREE_RE.search(l))), None)
    # Drop the name and degree lines; what's left is headline, location, action, summary...
    rest = [l for l in lines if l != name and not DEGREE_RE.fullmatch(l) and not l.startswith(name)]
    rest = [l for l in rest if l not in ACTION_LINES and not FOLLOWERS_RE.fullmatch(l)]
    headline = rest[0] if rest else None
    location = rest[1] if len(rest) > 1 else None
    summary = next((l for l in rest[2:] if len(l) > 60), None)
    return SearchResult(
        name=name,
        linkedin_url=f"https://www.linkedin.com/in/{slug}/",
        degree=degree,
        headline=headline,
        location=location,
        summary=summary,
    )


async def search_alumni(
    page: Page,
    company: Company,
    school_id: str,
    keywords: Optional[str] = None,
    max_pages: int = 2,
    delay_seconds: tuple[float, float] = (4, 8),
) -> list[SearchResult]:
    results: dict[str, SearchResult] = {}
    for page_number in range(1, max_pages + 1):
        params = {
            "currentCompany": _compact(company.ids),
            "schoolFilter": _compact([school_id]),
            "origin": "FACETED_SEARCH",
        }
        if keywords:
            params["keywords"] = keywords
        if page_number > 1:
            params["page"] = str(page_number)
            await asyncio.sleep(random.uniform(*delay_seconds))
        await _goto(page, "https://www.linkedin.com/search/results/people/?" + urlencode(params))
        cards = await page.evaluate(CARDS_JS)
        new = 0
        for card in cards:
            if card["slug"] not in results:
                results[card["slug"]] = _parse_card(**card)
                new += 1
        if new == 0:  # past the last page
            break
    return list(results.values())

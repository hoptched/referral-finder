"""Open internships from SimplifyJobs' GitHub list, for the "Jobs to apply to" column.

The repo's README is generated from .github/scripts/listings.json (~13 MB, every
listing ever). We download it at most once an hour and keep a trimmed copy of the
listings that match config.yaml's `simplify` filters.
"""

import json
import time

import httpx

from referral.config import project_path
from referral.jobs import USER_AGENT, clean_url

CACHE_PATH = project_path(".cache/simplify.json")
CACHE_SECONDS = 3600
DEFAULTS = {
    "listings_url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "terms": ["Summer 2027"],
    "categories": ["Software", "Software Engineering", "AI/ML/Data", "Data Science, AI & Machine Learning"],
}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("simplify") or {})}


def url_key(url: str) -> str:
    """Compare job links ignoring scheme, tracking params, trailing slash and case."""
    return clean_url(url).split("://", 1)[-1].rstrip("/").lower()


def trim(listings: list[dict], s: dict) -> list[dict]:
    """Open listings matching the configured terms and categories, newest first."""
    terms, categories = set(s["terms"]), set(s["categories"])
    jobs = [{
        "id": x["id"],
        "company": x["company_name"],
        "title": x["title"],
        "url": x["url"],
        "locations": x.get("locations") or [],
        "category": x.get("category"),
        "posted": x.get("date_posted") or x.get("date_updated") or 0,
    } for x in listings
        if x.get("active") and x.get("is_visible", True) and x.get("url")
        and terms & set(x.get("terms") or []) and x.get("category") in categories]
    return sorted(jobs, key=lambda j: j["posted"], reverse=True)


async def listings(cfg: dict, refresh: bool = False) -> dict:
    """{"fetched_at": epoch seconds, "jobs": [...]}, from the cache unless stale or refresh=True."""
    s = settings(cfg)
    try:
        cached = json.loads(CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cached = None
    fresh = cached and cached.get("filters") == s and time.time() - cached["fetched_at"] < CACHE_SECONDS
    if fresh and not refresh:
        return cached
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as http:
            response = await http.get(s["listings_url"])
            response.raise_for_status()
    except httpx.HTTPError:
        if cached and cached.get("filters") == s:  # offline or GitHub hiccup: an old list beats none
            return cached
        raise
    data = {"fetched_at": time.time(), "filters": s, "jobs": trim(response.json(), s)}
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))
    return data

"""Open internships from SimplifyJobs' GitHub list, for the "Jobs to apply to" column.

The repo's README is generated from .github/scripts/listings.json (~13 MB, every
listing ever). We download it at most once an hour and keep a trimmed copy of the
listings that match config.yaml's `simplify` filters.
"""

import json
import re
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
    "us_only": True,  # drop jobs with no location in the United States
    "exclude_phd": True,  # drop jobs only open to PhD students
    "max_age_days": 10,  # drop jobs posted longer ago than this
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
# Simplify's shorthand and country-level entries, beyond "City, ST" and bare state names.
US_PLACES = {"nyc", "sf", "south sf", "la", "united states", "usa", "us", "remote in usa", "remote in us", "remote"}
_US_NAMES = {n.lower() for n in US_STATES.values()} | US_PLACES


def in_us(location: str) -> bool:
    """Simplify writes US locations as "City, ST", a state name, or shorthand like "NYC";
    other countries end with the country ("Toronto, ON, Canada", "London, UK")."""
    loc = location.strip()
    if (m := re.search(r",\s*([A-Z]{2})$", loc)) and m.group(1) in US_STATES:
        return True
    return loc.lower() in _US_NAMES or loc.lower().endswith((", usa", ", united states"))


def phd_only(listing: dict) -> bool:
    """PhD is the only degree listed, or the title says it's a PhD internship."""
    degrees = set(listing.get("degrees") or [])
    return degrees == {"PhD"} or bool(re.search(r"\bph\.?\s?d\b", listing.get("title", ""), re.I))


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("simplify") or {})}


def url_key(url: str) -> str:
    """Compare job links ignoring scheme, tracking params, trailing slash and case."""
    return clean_url(url).split("://", 1)[-1].rstrip("/").lower()


def trim(listings: list[dict], s: dict) -> list[dict]:
    """Open listings matching the configured terms and categories, newest first."""
    terms, categories = set(s["terms"]), set(s["categories"])
    oldest = time.time() - s["max_age_days"] * 86400
    jobs = [{
        "id": x["id"],
        "company": x["company_name"],
        "title": x["title"],
        "url": x["url"],
        "locations": x.get("locations") or [],
        "category": x.get("category"),
        "posted": x.get("date_posted") or x.get("date_updated") or 0,
        "source": "Simplify",
    } for x in listings
        if x.get("active") and x.get("is_visible", True) and x.get("url")
        and terms & set(x.get("terms") or []) and x.get("category") in categories
        and (x.get("date_posted") or x.get("date_updated") or 0) >= oldest
        and not (s["exclude_phd"] and phd_only(x))
        # No location at all: kept, since there's nothing to judge by.
        and not (s["us_only"] and x.get("locations") and not any(in_us(l) for l in x["locations"]))]
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

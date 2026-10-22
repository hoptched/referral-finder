"""Parse a job posting URL into a JobPosting.

Applicant-tracking systems with public JSON APIs (SmartRecruiters, Greenhouse,
Lever, Ashby) are read directly. LinkedIn job URLs go through
linkedin_scraper's JobScraper (needs a session). Anything else is fetched and
structured by Claude.
"""

import html
import json
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from pydantic import BaseModel

from referral.llm import client, log_usage

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


class JobPosting(BaseModel):
    url: str
    source: str  # smartrecruiters | greenhouse | lever | ashby | workday | linkedin | json-ld | generic
    company: str
    title: str
    department: Optional[str] = None
    team: Optional[str] = None
    location: Optional[str] = None
    description: str = ""
    warnings: list[str] = []  # shown to the user, e.g. "this page may not be a job posting"


class _Extracted(BaseModel):
    is_job_posting: bool  # False for login walls, error pages, search results, "you applied" pages
    company: str
    title: str
    department: Optional[str]
    team: Optional[str]
    location: Optional[str]
    description: str


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr", "section"}
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


LEGAL_SUFFIX_RE = re.compile(r",?\s+(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|plc|gmbh)\.?$", re.I)


def clean_company(name: str) -> str:
    """'Zillow Group, Inc.' -> 'Zillow Group' (LinkedIn company pages drop legal suffixes)."""
    return LEGAL_SUFFIX_RE.sub("", name.strip()).strip()


def _slug_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


async def _get_json(http: httpx.AsyncClient, url: str):
    response = await http.get(url)
    response.raise_for_status()
    return response.json()


async def _smartrecruiters(http, url, company_id, posting_id) -> JobPosting:
    d = await _get_json(http, f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings/{posting_id}")
    sections = d.get("jobAd", {}).get("sections", {})
    description = "\n\n".join(
        f"{s['title']}\n{html_to_text(s['text'])}"
        for key in ("jobDescription", "qualifications", "additionalInformation")
        if (s := sections.get(key)) and s.get("text")
    )
    return JobPosting(
        url=url,
        source="smartrecruiters",
        company=d["company"]["name"],
        title=d["name"],
        department=(d.get("department") or {}).get("label"),
        location=(d.get("location") or {}).get("fullLocation"),
        description=description,
    )


async def _greenhouse(http, url, board, job_id) -> JobPosting:
    d = await _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?content=true")
    return JobPosting(
        url=url,
        source="greenhouse",
        company=d.get("company_name") or _slug_name(board),
        title=d["title"],
        department=", ".join(x["name"] for x in d.get("departments", [])) or None,
        location=(d.get("location") or {}).get("name"),
        # Greenhouse returns entity-escaped HTML.
        description=html_to_text(html.unescape(d.get("content", ""))),
    )


async def _lever(http, url, company, posting_id) -> JobPosting:
    d = await _get_json(http, f"https://api.lever.co/v0/postings/{company}/{posting_id}")
    cats = d.get("categories", {})
    description = "\n\n".join(
        [d.get("descriptionPlain", "")]
        + [f"{x['text']}\n{html_to_text(x['content'])}" for x in d.get("lists", [])]
        + [d.get("additionalPlain", "")]
    ).strip()
    return JobPosting(
        url=url,
        source="lever",
        company=_slug_name(company),
        title=d["text"],
        department=cats.get("department"),
        team=cats.get("team"),
        location=cats.get("location"),
        description=description,
    )


async def _ashby(http, url, org, job_id) -> JobPosting:
    board = await _get_json(http, f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    d = next((j for j in board["jobs"] if j["id"] == job_id), None)
    if d is None:
        raise ValueError(f"Ashby posting {job_id} not found on {org}'s board (closed?)")
    return JobPosting(
        url=url,
        source="ashby",
        company=_slug_name(org),
        title=d["title"],
        department=d.get("department"),
        team=d.get("team"),
        location=d.get("location"),
        description=d.get("descriptionPlain", ""),
    )


async def _workday(http, url, tenant, site, path) -> JobPosting:
    # Workday pages are rendered by JavaScript; the same data comes from its "cxs" JSON API.
    host = urlsplit(url).netloc
    response = await http.get(f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}",
                              headers={"Accept": "application/json"})
    response.raise_for_status()
    d = response.json()
    info = d["jobPostingInfo"]
    company = (d.get("hiringOrganization") or {}).get("name") or _slug_name(tenant)
    return JobPosting(
        url=url,
        source="workday",
        company=clean_company(company),
        title=info["title"],
        location=info.get("location"),
        description=html_to_text(info.get("jobDescription", "")),
    )


async def _linkedin(url, session_path) -> JobPosting:
    from linkedin_scraper import JobScraper

    from referral.linkedin import open_session

    async with open_session(session_path) as browser:
        j = await JobScraper(browser.page).scrape(url)
    return JobPosting(
        url=url,
        source="linkedin",
        company=j.company or "",
        title=j.job_title or "",
        location=j.location,
        description=j.job_description or "",
    )


def _json_ld_posting(url: str, markup: str) -> Optional[JobPosting]:
    """Read a schema.org JobPosting block, which many career sites embed for Google Jobs.
    Exact and free, and often the only place the description is (the page itself is JavaScript)."""
    for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', markup, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            types = item.get("@type")
            if "JobPosting" not in (types if isinstance(types, list) else [types]):
                continue
            org = item.get("hiringOrganization") or {}
            places = item.get("jobLocation") or []
            places = places if isinstance(places, list) else [places]
            address = (places[0].get("address") or {}) if places and isinstance(places[0], dict) else {}
            location = ", ".join(x for x in (address.get("addressLocality"), address.get("addressRegion")) if x) or None
            title = (item.get("title") or "").strip()
            company = clean_company(org.get("name") or "") if isinstance(org, dict) else ""
            if title and company:
                return JobPosting(url=url, source="json-ld", company=company, title=title, location=location,
                                  description=html_to_text(html.unescape(item.get("description") or "")))
    return None


MIN_PAGE_TEXT = 500  # characters; less means a bot wall, an empty shell, or a JavaScript-only page


async def _browser_html(url: str) -> str:
    """Load a page in headless Chromium (no LinkedIn session). Some career sites answer plain
    HTTP requests with an empty bot-check response (IBM: 202, no body) or render everything in JS."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(4_000)  # let the posting render
            return await page.content()
        finally:
            await browser.close()


async def _generic(http, url, model) -> JobPosting:
    response = await http.get(url, follow_redirects=True)
    markup = response.text if response.status_code == 200 else ""
    if posting := _json_ld_posting(url, markup):
        return posting
    if len(html_to_text(markup)) < MIN_PAGE_TEXT:
        markup = await _browser_html(url)
        if posting := _json_ld_posting(url, markup):
            return posting
    text = html_to_text(markup)
    extracted = await client().messages.parse(
        model=model,
        max_tokens=16000,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": "Below is the text of a web page that should be a job posting. First decide "
            "is_job_posting: true only if the page actually shows a specific job's posting (title plus "
            "description); false for login pages, error pages, search results, or application-status "
            "pages. If it is a posting, extract the hiring company, job title, department, team, location, "
            "and the full job description (responsibilities and qualifications, verbatim). Use null for "
            "anything not stated; don't guess.\n\n" + text,
        }],
        output_format=_Extracted,
    )
    log_usage("job_page", extracted)
    data = extracted.parsed_output
    if not (data.company.strip() and data.title.strip()):
        raise ValueError("Couldn't read the company and job title from this page. Try the job's public posting "
                         "link or its application link (Workday, Greenhouse, Lever, Ashby…).")
    warnings = []
    if not data.is_job_posting:
        # Lenient on purpose: the check can be wrong, so carry on but make sure the user sees it.
        # (Real case: an "applied jobs" link that showed a login form → title "Login".)
        warnings.append(f"This page may not be a job posting (it may need a login). Read it as “{data.title}” "
                        f"at {data.company}; check that's right before sending, and prefer the public posting link.")
    return JobPosting(url=url, source="generic", warnings=warnings, **data.model_dump(exclude={"is_job_posting"}))


# (host regex, path regex, handler). Host groups, then path groups, are passed to the handler.
_ROUTES = [
    (r"jobs\.smartrecruiters\.com", r"^/([^/]+)/(\d+)", _smartrecruiters),
    (r"(?:job-boards|boards)\.greenhouse\.io", r"^/([^/]+)/jobs/(\d+)", _greenhouse),
    # https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External/job/Seattle-WA-2nd-Ave/Title_P751630
    (r"([^.]+)\.wd\d+\.myworkdayjobs\.com", r"^/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+?)/?$", _workday),
    (r"jobs\.lever\.co", r"^/([^/]+)/([0-9a-f-]{36})", _lever),
    (r"jobs\.ashbyhq\.com", r"^/([^/]+)/([0-9a-f-]{36})", _ashby),
]


# Query parameters that only track where a click came from. Everything else is kept:
# some sites put the job itself in the query (IBM: ?jobId=137753, Greenhouse: gh_jid).
TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "src", "source",
                   "gh_src", "lever-source", "_hsenc", "_hsmi", "trk", "trackingid", "refid", "si"}


def clean_url(url: str) -> str:
    """Drop tracking parameters (utm_*, fbclid, ref, ...) and the fragment; keep the rest."""
    parts = urlsplit(url.strip())
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    query = f"?{urlencode(kept)}" if kept else ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}{query}"


async def parse_job(url: str, extract_model: str, session_path=None) -> JobPosting:
    url = clean_url(url)
    parts = urlsplit(url)
    if parts.netloc.endswith("simplify.jobs") and parts.path.startswith("/c/"):
        raise ValueError("That's a Simplify company page, not a job. Open the job and use its application link "
                         "(e.g. the myworkdayjobs.com page you applied on).")
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as http:
        for host, path, handler in _ROUTES:
            if (h := re.fullmatch(host, parts.netloc)) and (m := re.match(path, parts.path)):
                return await handler(http, url, *h.groups(), *m.groups())
        if "linkedin.com" in parts.netloc and "/jobs/" in parts.path:
            return await _linkedin(url, session_path)
        return await _generic(http, url, extract_model)

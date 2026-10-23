"""Jobs from Instagram stories (e.g. @zero2sudo posts application links daily).

Stories need a logged-in account and vanish after 24 hours, so a check runs once a
day (from `refer web` while it's open, or `refer ig-fetch` from a scheduler). Each
check opens the account's story page in the logged-in browser, like a person
tapping their story, and reads the story data Instagram's own page loads (no
direct API calls: those got the account rate limited), plus each story's image.
Link stickers come straight from the story data; Claude
(models.extract, with the image) decides which links are job applications and
reads the company and role. Stories already processed are never sent again.

Use a spare Instagram account for this (see README): scraping is against
Instagram's terms and an account can be locked for it.
"""

import asyncio
import base64
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import httpx
from linkedin_scraper import BrowserManager
from pydantic import BaseModel

from referral.config import project_path
from referral.jobs import USER_AGENT, clean_url
from referral.llm import client, log_usage

STATE_PATH = project_path(".data/instagram.json")
RAW_DIR = project_path(".cache/instagram")  # last fetch's raw story data, for debugging the parser
LOGIN_TIMEOUT_S = 300
DEFAULTS = {"accounts": ["zero2sudo"], "session_path": "ig_session.json", "check_every_hours": 24}


class InstagramLoginNeeded(RuntimeError):
    pass


class InstagramRateLimited(RuntimeError):
    pass


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("instagram") or {})}


def session_path(cfg: dict) -> Path:
    return project_path(settings(cfg)["session_path"])


# ---------- login ----------

async def create_session(path: Path) -> None:
    """Open a visible browser, wait for the user to log in to Instagram, save cookies."""
    async with BrowserManager(headless=False, user_agent=USER_AGENT) as browser:
        await browser.page.goto("https://www.instagram.com/accounts/login/")
        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            cookies = await browser.context.cookies("https://www.instagram.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                await asyncio.sleep(3)  # let the post-login redirects settle their cookies
                await browser.save_session(str(path))
                path.chmod(0o600)
                return
            await asyncio.sleep(2)
        raise TimeoutError("Not logged in to Instagram within 5 minutes.")


# ---------- fetching stories ----------

class Story(BaseModel):
    id: str
    account: str
    taken_at: int
    links: list[str]  # link-sticker URLs, Instagram's l.instagram.com wrapper removed
    image: Optional[bytes] = None  # the story's picture (or a video's cover frame), JPEG


def unwrap(url: str) -> str:
    """https://l.instagram.com/?u=<real url>&e=... -> the real url."""
    parts = urlsplit(url)
    if parts.netloc.endswith("l.instagram.com") and (u := parse_qs(parts.query).get("u")):
        return u[0]
    return url


def story_links(item: dict) -> list[str]:
    """Every link-sticker URL in one story item. Instagram has changed the sticker
    format over time (story_link_stickers, story_cta, ...), so look in any key that
    mentions a sticker or call-to-action and collect the http(s) URLs inside."""
    found: list[str] = []

    def walk(node, inside: bool):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, inside or "sticker" in k.lower() or "cta" in k.lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, inside)
        elif inside and isinstance(node, str) and node.startswith(("http://", "https://")):
            url = unwrap(node)
            host = urlsplit(url).netloc
            if not host.endswith(("cdninstagram.com", "fbcdn.net")) and url not in found:
                found.append(url)

    walk(item, False)
    return found


def _image_url(item: dict) -> Optional[str]:
    candidates = (item.get("image_versions2") or {}).get("candidates") or []
    # Largest first in Instagram's data; a mid-size one is plenty for reading text.
    sized = sorted(candidates, key=lambda c: c.get("width", 0))
    good = [c for c in sized if c.get("width", 0) >= 640]
    return (good[0] if good else sized[-1])["url"] if sized else None


def _story_id(item: dict) -> str:
    # pk is the story's own id; "id" is "<pk>_<owner id>".
    return str(item.get("pk") or str(item["id"]).split("_")[0])


def story_items(payload, account: str) -> list[dict]:
    """Story items for `account` anywhere in a JSON response from Instagram's page. An item
    has taken_at and image_versions2; it belongs to the user of the reel holding it (the
    viewer also preloads other accounts' stories, which are skipped)."""
    found: dict[str, dict] = {}

    def username(node) -> Optional[str]:
        user = node.get("user") or node.get("owner")
        return user.get("username") if isinstance(user, dict) else None

    def walk(node, owner: Optional[str]):
        if isinstance(node, dict):
            owner = username(node) or owner
            if "taken_at" in node and "image_versions2" in node and (node.get("pk") or node.get("id")):
                if owner is None or owner.lower() == account.lower():
                    found[_story_id(node)] = node
            for v in node.values():
                walk(v, owner)
        elif isinstance(node, list):
            for v in node:
                walk(v, owner)

    walk(payload, None)
    return list(found.values())


def _parse_json(text: str):
    text = text.removeprefix("for (;;);").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:  # some responses are several JSON objects, one per line
        out = []
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out


async def fetch_stories(path: Path, account: str) -> list[Story]:
    if not path.exists():
        raise InstagramLoginNeeded(f"No Instagram session at {path.name}. Run: refer ig-login")
    async with BrowserManager(headless=True, user_agent=USER_AGENT) as browser:
        await browser.load_session(str(path))
        page = browser.page
        payloads, statuses, pending = [], [], []

        async def grab(response):
            url = response.url
            if not urlsplit(url).netloc.endswith("instagram.com") or not ("/graphql" in url or "/api/" in url):
                return
            statuses.append(response.status)
            try:
                payloads.append(_parse_json(await response.text()))
            except Exception:
                pass

        page.on("response", lambda r: pending.append(asyncio.ensure_future(grab(r))))
        await page.goto(f"https://www.instagram.com/stories/{account}/", wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(4, 7))
        if "/accounts/login" in page.url or "/challenge" in page.url:
            raise InstagramLoginNeeded("Instagram session expired. Run: refer ig-login")
        # Instagram asks "View story?" before playing it.
        button = page.get_by_role("button", name=re.compile(r"view stor", re.I))
        if await button.count():
            await button.first.click()
            await asyncio.sleep(random.uniform(4, 7))
        await asyncio.gather(*pending, return_exceptions=True)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"{account}.json").write_text(json.dumps({"page": page.url, "statuses": statuses,
                                                             "responses": payloads}, indent=1))
        if 429 in statuses:
            raise InstagramRateLimited("Instagram is rate limiting this account; trying again in a day.")
        items = {}
        for payload in payloads:
            for item in story_items(payload, account):
                items[_story_id(item)] = item
        stories = []
        for item in items.values():
            image = None
            if url := _image_url(item):
                response = await page.request.get(url)
                image = await response.body() if response.ok else None
            stories.append(Story(id=_story_id(item), account=account,
                                 taken_at=int(item.get("taken_at") or time.time()),
                                 links=story_links(item), image=image))
            await asyncio.sleep(random.uniform(0.5, 1.5))
        await browser.save_session(str(path))  # keep the refreshed cookies
        return stories


# ---------- reading jobs out of a story ----------

class StoryJob(BaseModel):
    company: str
    title: str
    link: Optional[str]  # one of the story's links that leads to this job, else None
    locations: list[str]  # as shown, e.g. ["Seattle, WA"]; empty if not shown
    phd_only: bool  # the story says it's only for PhD students


class StoryJobs(BaseModel):
    jobs: list[StoryJob]


PROMPT = """This is an Instagram story from @{account}, who shares internship and new-grad job openings.
Links attached to the story (link stickers), with where each one ends up:
{links}

List each job opening the story advertises. For each: company, role title, locations if shown,
whether it's only for PhD students, and `link`: the link above (copy its final URL exactly) that
leads to that job or its application, or null if none does. Don't list things that aren't job
openings (courses, books, giveaways, sponsors, the creator's own content). If the story isn't
about a job opening, return an empty list."""


async def _resolve(url: str) -> str:
    """Follow redirects (bit.ly, lnkd.in, ...) to the page the link really opens."""
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True) as http:
            async with http.stream("GET", url) as response:
                return str(response.url)
    except httpx.HTTPError:
        return url


async def read_story(story: Story, model: str) -> list[dict]:
    """Jobs (in the "Jobs to apply to" shape) advertised by one story. Jobs without a link are dropped."""
    if not story.links and story.image is None:
        return []
    finals = [await _resolve(u) for u in story.links]
    content = []
    if story.image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(story.image).decode()}})
    links = "\n".join(f"- {u}" + (f" → {f}" if f != u else "") for u, f in zip(story.links, finals)) or "(none)"
    content.append({"type": "text", "text": PROMPT.format(account=story.account, links=links)})
    response = await client().messages.parse(
        model=model, max_tokens=4000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": content}], output_format=StoryJobs,
    )
    log_usage("instagram_story", response)
    allowed = {u: f for u, f in zip(story.links, finals)} | {f: f for f in finals}
    jobs = []
    for i, job in enumerate(response.parsed_output.jobs):
        if not job.link or job.link not in allowed:  # never trust a link that wasn't on the story
            continue
        jobs.append({"id": f"ig-{story.id}-{i}", "company": job.company, "title": job.title,
                     "url": clean_url(allowed[job.link]), "locations": job.locations, "category": None,
                     "posted": story.taken_at, "source": story.account, "phd_only": job.phd_only})
    return jobs


# ---------- daily check ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_run": None, "last_success": None, "error": None, "seen": {}, "jobs": []}


def visible_jobs(cfg: dict) -> list[dict]:
    """Saved story jobs that pass the same filters as the Simplify list (age, US, PhD)."""
    from referral.simplify import in_us, settings as list_settings

    s = list_settings(cfg)
    oldest = time.time() - s["max_age_days"] * 86400
    return [j for j in load_state()["jobs"]
            if j["posted"] >= oldest and not (s["exclude_phd"] and j.get("phd_only"))
            and not (s["us_only"] and j["locations"] and not any(in_us(l) for l in j["locations"]))]


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def due(cfg: dict, state: Optional[dict] = None) -> bool:
    """A check is due when the last successful one is older than check_every_hours. After a
    failure, wait 6 hours before trying again (a login problem won't fix itself sooner), or
    a full day after a rate limit (retrying sooner makes Instagram stricter)."""
    state = state or load_state()
    now = time.time()
    hours = settings(cfg)["check_every_hours"]
    ok = state.get("last_success") and now - datetime.fromisoformat(state["last_success"]).timestamp() < hours * 3600
    wait_hours = 24 if "rate limit" in (state.get("error") or "") else 6
    retry_wait = state.get("error") and state.get("last_run") and \
        now - datetime.fromisoformat(state["last_run"]).timestamp() < wait_hours * 3600
    return not ok and not retry_wait


async def check(cfg: dict, log=print) -> dict:
    """Fetch every live story of each configured account, read the new ones, save the jobs."""
    s = settings(cfg)
    state = load_state()
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    try:
        added = 0
        for account in s["accounts"]:
            stories = await fetch_stories(session_path(cfg), account)
            new = [st for st in stories if st.id not in state["seen"]]
            log(f"@{account}: {len(stories)} stories live, {len(new)} new")
            for story in new:
                jobs = await read_story(story, cfg["models"]["extract"])
                known = {j["url"] for j in state["jobs"]}
                fresh = [j for j in jobs if j["url"] not in known]
                state["jobs"].extend(fresh)
                added += len(fresh)
                state["seen"][story.id] = story.taken_at
                for j in fresh:
                    log(f"  + {j['company']}: {j['title']}  {j['url']}")
        # Stories live 24 h; remember ids a little longer than that, and jobs for 60 days.
        cutoff = time.time() - 3 * 86400
        state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
        state["jobs"] = [j for j in state["jobs"] if j["posted"] >= time.time() - 60 * 86400]
        state.update(error=None, last_success=state["last_run"], last_added=added)
        log(f"{added} new job{'s' if added != 1 else ''} added")
    except Exception as e:
        known = isinstance(e, (InstagramLoginNeeded, InstagramRateLimited))
        state["error"] = str(e) if known else f"{type(e).__name__}: {e}"
        log(f"Instagram check failed: {state['error']}")
    save_state(state)
    return state

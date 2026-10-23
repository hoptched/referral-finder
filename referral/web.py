"""Local web UI: queue jobs (dragged from the Simplify list or pasted), click people to select them.

Runs on 127.0.0.1 only. It drives your LinkedIn session and reads your
keys, so it must never listen on a public interface.
"""

import asyncio
import json
import os
import random
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from referral.compose import compose
from referral.llm import usage_entries
from referral.config import load_config, project_path, school_short
from referral import gmail
from referral.emails import (EmailResult, HunterBudget, apollo_cycle, apollo_enabled, hunter_enabled, apollo_used_this_cycle, find_email,
                             find_email_apollo, guess_email, infer_pattern, tried_methods)
from referral.jobs import clean_url, parse_job
from referral.pipeline import find_candidates, write_run
from referral import instagram, simplify, store
from referral.store import list_runs, load_run, run_path, save_run

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app):
    _kick()  # resume a queue left over from the last run
    loops = [] if os.environ.get("REFER_NO_BACKGROUND") else \
        [asyncio.create_task(_instagram_loop()), asyncio.create_task(_scheduler_loop())]
    yield
    for loop in loops:
        loop.cancel()


async def _instagram_loop():
    """While the site is open: check Instagram stories once a day (see referral/instagram.py).
    Nothing happens until `refer ig-login` has been run."""
    while True:
        try:
            cfg = load_config()
            if instagram.session_path(cfg).exists() and instagram.due(cfg):
                await instagram.check(cfg, log=lambda line: None)
        except Exception:
            pass  # check() records its own errors; anything else (bad config) just waits for the next round
        await asyncio.sleep(1800)


app = FastAPI(title="referral-finder", lifespan=_lifespan)

# LinkedIn work runs one at a time: a single browser session, and parallel
# scraping is exactly what gets accounts flagged.
_linkedin_lock = asyncio.Lock()
# Every run-file write goes through _update_run under this lock, re-reading the
# file first, so a slow request (drafting) can't overwrite a quick one (a click).
_run_lock = asyncio.Lock()
# Check-send-log for real emails happens under one lock so a double click can't send twice.
_send_lock = asyncio.Lock()
DRAFT_CONCURRENCY = 3


class QueueRequest(BaseModel):
    url: str
    keywords: Optional[str] = None
    company_url: Optional[str] = None
    simplify_id: Optional[str] = None  # set when dragged from the Simplify list
    company: Optional[str] = None  # shown until the posting has been read
    title: Optional[str] = None


class Selection(BaseModel):
    selected: list[str]


class Applied(BaseModel):
    applied: bool


class EmailEdit(BaseModel):
    linkedin_url: str
    email: Optional[str]


class DraftRequest(BaseModel):
    linkedin_url: Optional[str] = None  # one person (regenerate); None = every selected person without a draft


class ScheduleRequest(BaseModel):
    linkedin_url: str


class SendRequest(BaseModel):
    linkedin_url: str
    test: bool = False  # True: send to the connected Gmail account itself, not logged as sent


class DraftEdit(BaseModel):
    linkedin_url: str
    subject: str
    body: str


def _load(run_id: str) -> dict:
    if (run := load_run(run_id)) is None:
        raise HTTPException(404, "Run not found")
    run.setdefault("emails", {})
    run.setdefault("drafts", {})
    return run


async def _update_run(run_id: str, change) -> dict:
    async with _run_lock:
        run = _load(run_id)
        change(run)
        save_run(store.RUNS_DIR / f"{run_id}.json", run)
        return run


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/runs")
def runs():
    return list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return _load(run_id)


@app.put("/api/runs/{run_id}/selected")
async def set_selected(run_id: str, body: Selection):
    def change(run):
        known = {c["result"]["linkedin_url"] for c in run["candidates"]}
        run["selected"] = [u for u in body.selected if u in known]
    return {"selected": (await _update_run(run_id, change))["selected"]}


@app.put("/api/runs/{run_id}/applied")
async def set_applied(run_id: str, body: Applied):
    return {"applied": (await _update_run(run_id, lambda r: r.update(applied=body.applied)))["applied"]}


def _methods() -> list[str]:
    # Each source only if its key is set; guessing needs Hunter's verifier.
    guess = hunter_enabled() and load_config()["hunter"].get("guess_emails", False)
    return (["hunter"] if hunter_enabled() else []) + (["apollo"] if apollo_enabled() else []) + (["pattern"] if guess else [])


@app.post("/api/runs/{run_id}/emails")
async def lookup_emails(run_id: str):
    """Find work emails for selected people, trying each method at most once per person:

    1. Hunter finder (1 credit, only charged when it finds one)
    2. Apollo people match, if APOLLO_API_KEY is set (1 credit, only when it returns an email)
    3. Guess from the address format of emails already found at the company,
       checked by Hunter's verifier (0.5 credit per guess)
    A quota or auth error from a provider stops further calls to that provider.
    """
    cfg = load_config()
    run = _load(run_id)
    people = {c["result"]["linkedin_url"]: c["result"] for c in run["candidates"]}
    company = run["job"]["company"]
    emails = dict(run["emails"])
    found = {}
    budget = HunterBudget(cfg["hunter"]["monthly_credit_limit"])
    apollo_limit = cfg.get("apollo", {}).get("monthly_credit_limit", 50)
    stopped: set[str] = set()  # providers that hit a quota/auth error this request

    def domain():
        # Once any lookup has revealed the company's email domain, use it: more reliable than the name.
        return next((e["domain"] for e in emails.values() if e.get("domain")), None)

    def pending(url):
        e = emails.get(url) or {}
        return not e.get("email") and e.get("source") != "manual"

    def record(url, result, method, previous):
        entry = result.model_dump(mode="json")
        entry["tried"] = sorted(set(previous) | {method})
        emails[url] = found[url] = entry
        if result.error and not result.error.startswith("No email found") and result.error != "Name too short to guess":
            stopped.add(method)

    for method in ("hunter", "apollo"):
        if method not in _methods():
            continue
        for url in [u for u in run["selected"] if pending(u)]:
            previous = tried_methods(emails.get(url) or {})
            if method in previous or method in stopped:
                continue
            name = people[url]["name"]
            if method == "hunter":
                result = await find_email(name, url, company, budget, domain())
            else:
                # Apollo's non-"verified" emails (e.g. "extrapolated", its own format guess) are kept
                # unchecked: the user prefers an occasional bounce over spending Hunter credits.
                result = await find_email_apollo(name, url, company, apollo_limit, domain())
            record(url, result, method, previous)

    patterns = [p for u, e in emails.items() if e.get("email") and e["source"] != "manual"
                and (p := infer_pattern(people[u]["name"] if u in people else "", e["email"]))]
    if domain() and "pattern" in _methods():
        for url in [u for u in run["selected"] if pending(u)]:
            previous = tried_methods(emails.get(url) or {})
            if "pattern" in previous or "pattern" in stopped or "hunter" in stopped:
                continue
            record(url, await guess_email(people[url]["name"], domain(), patterns, budget), "pattern", previous)

    run = await _update_run(run_id, lambda r: r["emails"].update(found))
    return {"emails": run["emails"]}


@app.get("/api/email-sources")
def email_sources():
    cfg = load_config()
    return {"methods": _methods(), "apollo": {"enabled": apollo_enabled(), "used": apollo_used_this_cycle(),
                                              "limit": cfg.get("apollo", {}).get("monthly_credit_limit", 50)}}


@app.get("/api/settings")
def settings():
    cfg = load_config()
    me = cfg["me"]
    return {"school": me["school"], "school_short": school_short(me["school"]),
            "next_send_at": next_send_at(cfg).isoformat()}


@app.get("/api/credits")
async def credits():
    """Everything the header's credit panel shows. Each part fails independently."""
    cfg = load_config()
    out = {}
    if not hunter_enabled():
        out["hunter"] = {"enabled": False}
    else:
        try:
            out["hunter"] = {"enabled": True, **await HunterBudget(cfg["hunter"]["monthly_credit_limit"]).status()}
        except Exception as e:
            out["hunter"] = {"enabled": True, "error": f"{type(e).__name__}: {e}"}
    limit = cfg.get("apollo", {}).get("monthly_credit_limit", 50)
    out["apollo"] = {"enabled": apollo_enabled(), "used": apollo_used_this_cycle(), "limit": limit,
                     "left": max(limit - apollo_used_this_cycle(), 0), "resets": apollo_cycle()[1].isoformat()}
    day_limit = cfg["linkedin"]["max_profiles_per_day"]
    today = date.today()
    out["linkedin"] = {"limit": day_limit, "left": max(day_limit - store.scrapes_today(), 0),
                       "resets": (today + timedelta(days=1)).isoformat()}
    month = datetime.now().strftime("%Y-%m")
    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    out["claude"] = {"usd_month": round(sum(e["usd"] for e in usage_entries() if e["at"].startswith(month)), 2),
                     "resets": next_month.isoformat()}
    out["gmail"] = gmail.status()
    return out


@app.get("/api/hunter")
async def hunter_status():
    try:
        return await HunterBudget(load_config()["hunter"]["monthly_credit_limit"]).status()
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach Hunter: {e}")


@app.get("/api/gmail")
def gmail_status():
    return gmail.status()


@app.post("/api/runs/{run_id}/send")
async def send_email(run_id: str, body: SendRequest):
    """Send one drafted email now (or a [TEST] copy to yourself)."""
    return await _deliver(run_id, body.linkedin_url, body.test)


async def _deliver(run_id: str, url: str, test: bool = False) -> dict:
    """Send one drafted email through Gmail, resume attached. Never twice to the same person.
    Used by "Send now" and by the scheduler; a real send clears that person's schedule."""
    cfg = load_config()
    run = _load(run_id)
    draft = run["drafts"].get(url)
    if draft is None:
        raise HTTPException(400, "No draft for that person yet")
    to = (run["emails"].get(url) or {}).get("email")
    if not test and not to:
        raise HTTPException(400, "No email address for that person")
    async with _send_lock:
        if not test and (previous := store.already_sent(url, to)):
            raise HTTPException(409, f"Already emailed {previous['to']} on {previous['sent_at'][:10]}")
        me = cfg["me"]
        try:
            recipient = gmail.account_email() if test else to
            message = gmail.build_message(
                recipient,
                ("[TEST] " if test else "") + draft["subject"],
                draft["body"],
                project_path(me["resume_path"]),
                f"{me['name'].replace(' ', '_')}_Resume.pdf",
            )
            message_id = await asyncio.to_thread(gmail.send, message)
        except gmail.GmailNotConnected as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(502, f"Gmail send failed: {type(e).__name__}: {e}")

        entry = {"sent_at": datetime.now().isoformat(), "to": recipient, "linkedin_url": url, "run_id": run_id,
                 "subject": draft["subject"], "gmail_id": message_id, "test": test}
        if test:
            return {"sent": entry}
        store.log_sent(entry)

    def change(r):
        r.setdefault("sent", {})[url] = entry
        (r.get("scheduled") or {}).pop(url, None)
    run = await _update_run(run_id, change)
    return {"sent": entry, "run_sent": run["sent"], "scheduled": run.get("scheduled", {})}


# ---------- scheduled sending ----------
# Gmail's API has no scheduled send, so emails wait in run["scheduled"] and the site sends
# them at email.send_time (default 11:00, email.timezone). Missed by more than
# SEND_WINDOW_HOURS (the PC was off), an email moves to the next day instead of going out late.
SEND_WINDOW_HOURS = 3


def _send_settings(cfg: dict) -> dict:
    return {"send_time": "11:00", "timezone": "America/New_York", **(cfg.get("email") or {})}


def next_send_at(cfg: dict, now: Optional[datetime] = None) -> datetime:
    s = _send_settings(cfg)
    tz = ZoneInfo(s["timezone"])
    now = (now or datetime.now(tz)).astimezone(tz)
    hour, minute = (int(x) for x in s["send_time"].split(":"))
    day = now.date()
    at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if at <= now:
        day += timedelta(days=1)
        at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return at


def _scheduled_elsewhere(run_id: str, url: str, to: str) -> Optional[str]:
    """The company of another search where this person (or address) is already scheduled."""
    for path in store.RUNS_DIR.glob("*.json"):
        other = json.loads(path.read_text())
        for u, e in (other.get("scheduled") or {}).items():
            if (path.stem, u) != (run_id, url) and (u == url or e["to"].lower() == to.lower()):
                return other["job"]["company"]
    return None


@app.post("/api/runs/{run_id}/schedule")
async def schedule_email(run_id: str, body: ScheduleRequest):
    """Queue one drafted email for the next send time. The draft is read when it's sent, so
    edits made before then go out."""
    cfg = load_config()
    run = _load(run_id)
    url = body.linkedin_url
    if url not in run["drafts"]:
        raise HTTPException(400, "No draft for that person yet")
    to = (run["emails"].get(url) or {}).get("email")
    if not to:
        raise HTTPException(400, "No email address for that person")
    if previous := store.already_sent(url, to):
        raise HTTPException(409, f"Already emailed {previous['to']} on {previous['sent_at'][:10]}")
    if company := _scheduled_elsewhere(run_id, url, to):
        raise HTTPException(409, f"{to} is already scheduled from the {company} search")
    entry = {"send_at": next_send_at(cfg).isoformat(), "to": to, "scheduled_at": datetime.now().isoformat(),
             "error": None}
    run = await _update_run(run_id, lambda r: r.setdefault("scheduled", {}).update({url: entry}))
    return {"scheduled": run["scheduled"]}


@app.post("/api/runs/{run_id}/unschedule")
async def unschedule_email(run_id: str, body: ScheduleRequest):
    run = await _update_run(run_id, lambda r: (r.get("scheduled") or {}).pop(body.linkedin_url, None))
    return {"scheduled": run.get("scheduled", {})}


async def _send_due(now: Optional[datetime] = None) -> None:
    """Send every scheduled email whose time has come, 20–60 s apart."""
    cfg = load_config()
    now = now or datetime.now(ZoneInfo(_send_settings(cfg)["timezone"]))
    first = True
    for path in sorted(store.RUNS_DIR.glob("*.json")):
        run = json.loads(path.read_text())
        for url, e in list((run.get("scheduled") or {}).items()):
            at = datetime.fromisoformat(e["send_at"])
            if e.get("error") or at > now:
                continue
            if now - at > timedelta(hours=SEND_WINDOW_HOURS):
                later = next_send_at(cfg, now).isoformat()
                await _update_run(path.stem, lambda r, u=url, t=later: r["scheduled"][u].update(send_at=t))
                continue
            if not first:
                await asyncio.sleep(random.uniform(20, 60))
            first = False
            try:
                await _deliver(path.stem, url)
            except HTTPException as ex:
                if ex.status_code == 409:  # sent some other way meanwhile
                    await _update_run(path.stem, lambda r, u=url: r["scheduled"].pop(u, None))
                else:  # shown on the card; the user can Send now or cancel
                    await _update_run(path.stem, lambda r, u=url, d=ex.detail: r["scheduled"][u].update(error=str(d)))


async def _scheduler_loop():
    while True:
        try:
            await _send_due()
        except Exception:
            pass  # a bad run file must not stop scheduled sending; the next round tries again
        await asyncio.sleep(30)


@app.put("/api/runs/{run_id}/emails")
async def set_email(run_id: str, body: EmailEdit):
    def change(run):
        email = (body.email or "").strip() or None
        run["emails"][body.linkedin_url] = EmailResult(
            email=email, source="manual", error=None if email else "No email", found_at=datetime.now()
        ).model_dump(mode="json")
        if body.linkedin_url in run["drafts"]:
            run["drafts"][body.linkedin_url]["to"] = email
    return {"emails": (await _update_run(run_id, change))["emails"]}


def _email_of(run: dict, url: str):
    return (run["emails"].get(url) or {}).get("email")


@app.post("/api/runs/{run_id}/drafts")
async def make_drafts(run_id: str, body: DraftRequest):
    """Write email drafts with Claude: one person (regenerate), or every selected person who has
    an email address but no draft. People without an email are never drafted."""
    cfg = load_config()
    run = _load(run_id)
    candidates = {c["result"]["linkedin_url"]: c for c in run["candidates"]}
    if body.linkedin_url:
        if body.linkedin_url not in candidates:
            raise HTTPException(400, "Unknown person")
        if not _email_of(run, body.linkedin_url):
            raise HTTPException(400, "No email for that person")
        targets = [body.linkedin_url]
    else:
        targets = [u for u in run["selected"] if u not in run["drafts"] and _email_of(run, u)]
    if not targets:
        return {"drafts": run["drafts"]}
    limit = asyncio.Semaphore(DRAFT_CONCURRENCY)

    async def one(url):
        async with limit:
            to = (run["emails"].get(url) or {}).get("email")
            return url, (await compose(cfg, run["job"], candidates[url], to)).model_dump(mode="json")

    try:
        drafts = dict(await asyncio.gather(*(one(u) for u in targets)))
    except Exception as e:
        raise HTTPException(502, f"Drafting failed: {type(e).__name__}: {e}")
    run = await _update_run(run_id, lambda r: r["drafts"].update(drafts))
    return {"drafts": run["drafts"]}


@app.post("/api/runs/{run_id}/outreach")
async def prepare_outreach(run_id: str):
    """What opening the Outreach tab does: look up emails, then draft emails for people who have
    one. Each step only does missing work."""
    errors = []
    if not _load(run_id)["selected"]:
        raise HTTPException(400, "Select people first")
    try:
        await lookup_emails(run_id)
    except HTTPException as e:
        errors.append(f"Email lookup: {e.detail}")
    try:
        await make_drafts(run_id, DraftRequest())
    except HTTPException as e:
        errors.append(str(e.detail))
    return {"run": _load(run_id), "errors": errors}


@app.put("/api/runs/{run_id}/drafts")
async def edit_draft(run_id: str, body: DraftEdit):
    def change(run):
        if body.linkedin_url not in run["drafts"]:
            raise HTTPException(404, "No draft for that person")
        run["drafts"][body.linkedin_url].update(subject=body.subject, body=body.body, edited=True)
    return {"drafts": (await _update_run(run_id, change))["drafts"]}


@app.get("/api/jobs")
async def jobs(refresh: bool = False):
    """Simplify's open listings plus jobs from Instagram stories, minus jobs that already have a
    search or are in the queue."""
    cfg = load_config()
    try:
        data = await simplify.listings(cfg, refresh)
    except Exception as e:
        raise HTTPException(502, f"Couldn't load the Simplify list: {type(e).__name__}: {e}")
    # A story job that's also on Simplify shows once, as the Simplify job tagged with the account.
    listed = {simplify.url_key(j["url"]): j for j in data["jobs"]}
    jobs = [dict(j) for j in data["jobs"]]
    by_key = {simplify.url_key(j["url"]): j for j in jobs}
    for j in instagram.visible_jobs(cfg):
        key = simplify.url_key(j["url"])
        if key in listed:
            by_key[key].setdefault("also", []).append(j["source"])
        else:
            jobs.append(j)
            by_key[key] = j
    jobs.sort(key=lambda j: j["posted"], reverse=True)
    runs = list_runs()
    taken_ids = {r["simplify_id"] for r in runs if r.get("simplify_id")} | {i["simplify_id"] for i in _queue()}
    taken_urls = {simplify.url_key(u) for u in [r["url"] for r in runs] + [i["url"] for i in _queue()]}
    remaining = [j for j in jobs if j["id"] not in taken_ids and simplify.url_key(j["url"]) not in taken_urls]
    ig = instagram.load_state()
    return {"fetched_at": data["fetched_at"], "jobs": remaining, "hidden": len(jobs) - len(remaining),
            "instagram": {"accounts": instagram.settings(cfg)["accounts"],
                          "logged_in": instagram.session_path(cfg).exists(),
                          "last_success": ig.get("last_success"), "error": ig.get("error")}}


# Searches run one after another from this queue. It's saved in .data/queue.json so a
# restart doesn't lose it; finished items leave the queue (they're runs now).
QUEUE_PATH = project_path(".data/queue.json")
_queue_items: Optional[list[dict]] = None
_finished: dict[str, str] = {}  # queue item id -> run id, so the page that pasted a link can open it
_worker: Optional[asyncio.Task] = None


def _queue() -> list[dict]:
    global _queue_items
    if _queue_items is None:
        try:
            _queue_items = json.loads(QUEUE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            _queue_items = []
        for item in _queue_items:  # interrupted by a restart: run it again
            if item["status"] == "running":
                item.update(status="queued", log=[])
    return _queue_items


def _save_queue() -> None:
    QUEUE_PATH.parent.mkdir(exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(_queue(), indent=2))


def _kick() -> None:
    # Must run on the event loop: every caller is an async endpoint (a plain `def` endpoint runs
    # in a worker thread, where create_task fails and a retried search would sit queued forever).
    global _worker
    if (_worker is None or _worker.done()) and any(i["status"] == "queued" for i in _queue()):
        _worker = asyncio.create_task(_work())


@app.get("/api/queue")
async def get_queue():
    _kick()
    return {"items": _queue(), "finished": _finished}


@app.post("/api/queue")
async def add_to_queue(body: QueueRequest):
    url = clean_url(body.url)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "That doesn't look like a job link")
    if any(simplify.url_key(i["url"]) == simplify.url_key(url) for i in _queue()):
        raise HTTPException(409, "That job is already in the queue")
    item = {"id": uuid.uuid4().hex, **body.model_dump(), "url": url, "status": "queued", "log": [], "error": None,
            "added_at": datetime.now().isoformat()}
    _queue().append(item)
    _save_queue()
    _kick()
    return item


def _queue_item(item_id: str) -> dict:
    if (item := next((i for i in _queue() if i["id"] == item_id), None)) is None:
        raise HTTPException(404, "Not in the queue")
    return item


@app.delete("/api/queue/{item_id}")
async def remove_from_queue(item_id: str):
    item = _queue_item(item_id)
    if item["status"] == "running":
        raise HTTPException(409, "That search is running; wait for it to finish")
    _queue().remove(item)
    _save_queue()
    return {"items": _queue()}


@app.post("/api/queue/{item_id}/retry")
async def retry_queue_item(item_id: str):
    item = _queue_item(item_id)
    if item["status"] == "error":
        item.update(status="queued", error=None, log=[])
        _save_queue()
        _kick()
    return item


async def _work():
    while item := next((i for i in _queue() if i["status"] == "queued"), None):
        item.update(status="running", log=[])
        _save_queue()
        try:
            _finished[item["id"]] = await _run_find(item)
            _queue().remove(item)
        except Exception as e:  # shown on the queue item instead of a silent dead search
            item.update(status="error", error=f"{type(e).__name__}: {e}")
        _save_queue()


async def _run_find(item: dict) -> str:
    def log(line):
        item["log"].append(line)
        _save_queue()

    async with _linkedin_lock:
        cfg = load_config()
        log("Reading the job posting…")
        posting = await parse_job(item["url"], cfg["models"]["extract"], project_path(cfg["linkedin"]["session_path"]))
        item.update(company=posting.company, title=posting.title)
        log(f"{posting.title} @ {posting.company}")
        for warning in posting.warnings:
            log(f"⚠ {warning}")
        candidates = await find_candidates(cfg, posting, item.get("company_url"), item.get("keywords"), log=log)
        # Re-running a job keeps everything else about it: picks, emails, drafts, sends, Applied.
        run_id = run_path(posting.company, posting.url).stem
        async with _run_lock:
            previous = load_run(run_id) or {}
            write_run(posting, candidates, previous.get("selected", []))
        kept = {k: v for k, v in previous.items() if k not in ("created_at", "job", "candidates", "selected")}
        if item.get("simplify_id"):
            kept["simplify_id"] = item["simplify_id"]
        if kept:
            await _update_run(run_id, lambda r: r.update(kept))
        log("Done.")
        return run_id

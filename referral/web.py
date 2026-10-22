"""Local web UI: paste a job link, watch progress, click people to select them.

Runs on 127.0.0.1 only. It drives your LinkedIn session and reads your
keys, so it must never listen on a public interface.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from referral.compose import compose, job_names, render_invitation
from referral.llm import usage_entries
from referral.config import load_config, project_path, school_short
from referral import gmail
from referral.emails import (EmailResult, HunterBudget, apollo_cycle, apollo_enabled, apollo_used_this_cycle, find_email,
                             find_email_apollo, guess_email, infer_pattern, tried_methods)
from referral.jobs import parse_job
from referral.pipeline import find_candidates, write_run
from referral import store
from referral.store import list_runs, load_run, run_path, save_run

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="referral-finder")

# LinkedIn work runs one at a time: a single browser session, and parallel
# scraping is exactly what gets accounts flagged.
_linkedin_lock = asyncio.Lock()
# Every run-file write goes through _update_run under this lock, re-reading the
# file first, so a slow request (drafting) can't overwrite a quick one (a click).
_run_lock = asyncio.Lock()
# Check-send-log for real emails happens under one lock so a double click can't send twice.
_send_lock = asyncio.Lock()
_tasks: dict[str, dict] = {}
DRAFT_CONCURRENCY = 3


class FindRequest(BaseModel):
    url: str
    keywords: Optional[str] = None
    company_url: Optional[str] = None


class Selection(BaseModel):
    selected: list[str]


class EmailEdit(BaseModel):
    linkedin_url: str
    email: Optional[str]


class DraftRequest(BaseModel):
    linkedin_url: Optional[str] = None  # one person (regenerate); None = every selected person without a draft


class SendRequest(BaseModel):
    linkedin_url: str
    test: bool = False  # True: send to the connected Gmail account itself, not logged as sent


class InvitationEdit(BaseModel):
    linkedin_url: str
    text: str


class DraftEdit(BaseModel):
    linkedin_url: str
    subject: str
    body: str
    invitation: str


def _load(run_id: str) -> dict:
    if (run := load_run(run_id)) is None:
        raise HTTPException(404, "Run not found")
    run.setdefault("emails", {})
    run.setdefault("drafts", {})
    run.setdefault("invitations", {})
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


def _methods() -> list[str]:
    guess = load_config()["hunter"].get("guess_emails", False)
    return ["hunter"] + (["apollo"] if apollo_enabled() else []) + (["pattern"] if guess else [])


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
    me = load_config()["me"]
    return {"school": me["school"], "school_short": school_short(me["school"])}


@app.get("/api/credits")
async def credits():
    """Everything the header's credit panel shows. Each part fails independently."""
    cfg = load_config()
    out = {}
    try:
        out["hunter"] = await HunterBudget(cfg["hunter"]["monthly_credit_limit"]).status()
    except Exception as e:
        out["hunter"] = {"error": f"{type(e).__name__}: {e}"}
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
    """Send one drafted email through Gmail, resume attached. Never twice to the same person."""
    cfg = load_config()
    run = _load(run_id)
    url = body.linkedin_url
    draft = run["drafts"].get(url)
    if draft is None:
        raise HTTPException(400, "No draft for that person yet")
    to = (run["emails"].get(url) or {}).get("email")
    if not body.test and not to:
        raise HTTPException(400, "No email address for that person")
    async with _send_lock:
        if not body.test and (previous := store.already_sent(url, to)):
            raise HTTPException(409, f"Already emailed {previous['to']} on {previous['sent_at'][:10]}")
        me = cfg["me"]
        try:
            recipient = gmail.account_email() if body.test else to
            message = gmail.build_message(
                recipient,
                ("[TEST] " if body.test else "") + draft["subject"],
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
                 "subject": draft["subject"], "gmail_id": message_id, "test": body.test}
        if body.test:
            return {"sent": entry}
        store.log_sent(entry)
    run = await _update_run(run_id, lambda r: r.setdefault("sent", {}).update({url: entry}))
    return {"sent": entry, "run_sent": run["sent"]}


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
    an email address but no draft. People without an email are never drafted (LinkedIn route)."""
    cfg = load_config()
    run = _load(run_id)
    candidates = {c["result"]["linkedin_url"]: c for c in run["candidates"]}
    if body.linkedin_url:
        if body.linkedin_url not in candidates:
            raise HTTPException(400, "Unknown person")
        if not _email_of(run, body.linkedin_url):
            raise HTTPException(400, "No email for that person; use the LinkedIn invite instead")
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


@app.put("/api/runs/{run_id}/invitations")
async def edit_invitation(run_id: str, body: InvitationEdit):
    run = await _update_run(run_id, lambda r: r["invitations"].update({body.linkedin_url: body.text}))
    return {"invitations": run["invitations"]}


@app.post("/api/runs/{run_id}/outreach")
async def prepare_outreach(run_id: str):
    """What opening the Outreach tab does: look up emails, then draft emails for people who have
    one and write LinkedIn invites for people who don't. Each step only does missing work."""
    cfg = load_config()
    errors = []
    if not _load(run_id)["selected"]:
        raise HTTPException(400, "Select people first")
    try:
        await lookup_emails(run_id)
    except HTTPException as e:
        errors.append(f"Email lookup: {e.detail}")

    run = _load(run_id)
    if "job_names" not in run:
        try:
            names = (await job_names(run["job"], cfg["models"]["extract"])).model_dump()
        except Exception as e:  # fall back to the posting's own names
            names = {"short_title": run["job"]["title"], "company": run["job"]["company"]}
            errors.append(f"Short job name: {type(e).__name__}: {e}")
        run = await _update_run(run_id, lambda r: r.update(job_names=names))
    people = {c["result"]["linkedin_url"]: c["result"]["name"] for c in run["candidates"]}
    invites = {u: render_invitation(cfg, run["job_names"], people[u])
               for u in run["selected"] if not _email_of(run, u) and u not in run["invitations"]}
    if invites:
        await _update_run(run_id, lambda r: r["invitations"].update(invites))

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
        run["drafts"][body.linkedin_url].update(subject=body.subject, body=body.body, invitation=body.invitation,
                                                edited=True)
    return {"drafts": (await _update_run(run_id, change))["drafts"]}


@app.post("/api/find")
async def start_find(body: FindRequest):
    if _linkedin_lock.locked():
        raise HTTPException(409, "A search is already running. Wait for it to finish.")
    task_id = uuid.uuid4().hex
    _tasks[task_id] = {"status": "running", "log": [], "run_id": None, "error": None}
    asyncio.create_task(_run_find(task_id, body))
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    if (task := _tasks.get(task_id)) is None:
        raise HTTPException(404, "Task not found")
    return task


async def _run_find(task_id: str, body: FindRequest):
    task = _tasks[task_id]
    log = task["log"].append
    async with _linkedin_lock:
        try:
            cfg = load_config()
            log("Reading the job posting…")
            posting = await parse_job(body.url, cfg["models"]["extract"], project_path(cfg["linkedin"]["session_path"]))
            log(f"{posting.title} @ {posting.company}")
            for warning in posting.warnings:
                log(f"⚠ {warning}")
            candidates = await find_candidates(cfg, posting, body.company_url, body.keywords, log=log)
            # Re-running a job keeps the people already picked, their emails and drafts.
            run_id = run_path(posting.company, posting.url).stem
            async with _run_lock:
                previous = load_run(run_id) or {}
                write_run(posting, candidates, previous.get("selected", []))
            if previous.get("emails") or previous.get("drafts"):
                await _update_run(run_id, lambda r: r.update(emails=previous.get("emails", {}),
                                                             drafts=previous.get("drafts", {})))
            task["run_id"] = run_id
            log("Done.")
            task["status"] = "done"
        except Exception as e:  # surfaced in the UI instead of a silent dead task
            task["status"] = "error"
            task["error"] = f"{type(e).__name__}: {e}"

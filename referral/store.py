"""Local JSON storage: profile cache, daily scrape counter, saved runs.

All of it lives under gitignored directories (.cache/, runs/).
"""

import json
import re
from datetime import date, datetime, timedelta

from referral.config import project_path
from referral.profiles import Profile

PROFILE_DIR = project_path(".cache/profiles")
SCRAPE_LOG = project_path(".cache/scrape_log.json")
RUNS_DIR = project_path("runs")
PROFILE_TTL = timedelta(days=30)


def _slug(linkedin_url: str) -> str:
    return linkedin_url.rstrip("/").rsplit("/", 1)[-1]


def cached_profile(linkedin_url: str):
    path = PROFILE_DIR / f"{_slug(linkedin_url)}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if datetime.now() - datetime.fromisoformat(data["scraped_at"]) > PROFILE_TTL:
        return None
    return Profile(**data["profile"])


def save_profile(profile: Profile) -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILE_DIR / f"{_slug(profile.linkedin_url)}.json"
    path.write_text(json.dumps({"scraped_at": datetime.now().isoformat(), "profile": profile.model_dump()}, indent=2))


def scrapes_today() -> int:
    try:
        return json.loads(SCRAPE_LOG.read_text()).get(date.today().isoformat(), 0)
    except FileNotFoundError:
        return 0


def record_scrape() -> None:
    try:
        log = json.loads(SCRAPE_LOG.read_text())
    except FileNotFoundError:
        log = {}
    today = date.today().isoformat()
    log[today] = log.get(today, 0) + 1
    SCRAPE_LOG.parent.mkdir(parents=True, exist_ok=True)
    SCRAPE_LOG.write_text(json.dumps(log, indent=2))


def run_path(company: str, job_url: str):
    job_id = re.sub(r"[^A-Za-z0-9]+", "-", job_url.rstrip("/").rsplit("/", 1)[-1])[:40]
    company_slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    return RUNS_DIR / f"{company_slug}-{job_id}.json"


def save_run(path, data: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def load_run(run_id: str):
    path = RUNS_DIR / f"{run_id}.json"
    if not re.fullmatch(r"[A-Za-z0-9-]+", run_id) or not path.exists():
        return None
    return json.loads(path.read_text())


def outreach_status(run: dict) -> dict:
    """How far outreach got. "Done" = everyone selected has an email draft or a LinkedIn invite."""
    selected = run.get("selected", [])
    drafts, invites, sent = run.get("drafts", {}), run.get("invitations", {}), run.get("sent", {})
    emails = run.get("emails", {})
    has_email = {u for u in selected if (emails.get(u) or {}).get("email")}
    ready = [u for u in selected if u in drafts or u in invites]
    # Old runs have drafts for people without an email; those count as LinkedIn outreach now.
    emailed = [u for u in ready if u in drafts and u in has_email]
    return {
        "drafted": len(emailed),
        "invites": len(ready) - len(emailed),
        "sent": sum(u in sent for u in selected),
        "done": bool(selected) and len(ready) == len(selected),
    }


def list_runs() -> list[dict]:
    runs = []
    for path in sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = json.loads(path.read_text())
        runs.append({
            "id": path.stem,
            "company": data["job"]["company"],
            "title": data["job"]["title"],
            "created_at": data["created_at"],
            "candidates": len(data["candidates"]),
            "selected": len(data["selected"]),
            "outreach": outreach_status(data),
        })
    return runs


# Every real send, across all runs, one JSON object per line. Used to never
# email the same person twice. Lives in .data/ (gitignored), not .cache/,
# because it must not be cleared.
SENT_LOG = project_path(".data/sent.jsonl")


def sent_log() -> list[dict]:
    try:
        return [json.loads(line) for line in SENT_LOG.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def already_sent(linkedin_url: str, email: str) -> dict | None:
    email = email.lower()
    return next((e for e in sent_log() if e["linkedin_url"] == linkedin_url or e["to"].lower() == email), None)


def log_sent(entry: dict) -> None:
    SENT_LOG.parent.mkdir(exist_ok=True)
    with SENT_LOG.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

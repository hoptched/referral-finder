"""Find a person's work email with Hunter.io.

Uses Hunter's email-finder with the person's full name, the company (name, or
domain once one lookup has revealed it) and their LinkedIn handle, which
Hunter uses to pick the right person. Each call costs one Hunter search
credit, so results are stored in the run and never looked up twice.
"""

import calendar
import json
import os
import re
from datetime import date, datetime
from typing import Optional

import httpx
from pydantic import BaseModel

from referral import config  # noqa: F401  (loads .env)
from referral.config import load_config, project_path

HUNTER_URL = "https://api.hunter.io/v2/email-finder"
ACCOUNT_URL = "https://api.hunter.io/v2/account"
FIND_COST = 1.0  # credits, charged only when an email is found
VERIFY_COST = 0.5  # credits per verification (shares the same monthly pool)


class BudgetExceeded(Exception):
    pass


class HunterBudget:
    """Never let a lookup push this month's Hunter usage past `limit` credits.

    Usage is read from Hunter's (free) account endpoint before every paid call,
    so it's correct even across restarts and lookups made outside this tool.
    """

    def __init__(self, limit: float):
        self.limit = limit

    async def status(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(ACCOUNT_URL, params={"api_key": os.environ["HUNTER_API_KEY"]})
        response.raise_for_status()
        data = response.json()["data"]
        credits = data["requests"]["credits"]
        cap = min(self.limit, credits["available"])
        return {"used": credits["used"], "limit": cap, "left": max(cap - credits["used"], 0),
                "resets": data.get("reset_date")}

    async def spend(self, cost: float) -> None:
        status = await self.status()
        if status["left"] < cost:
            raise BudgetExceeded(f"Hunter limit reached: {status['used']:g} of {status['limit']:g} credits used "
                                 f"this month (resets {status['resets']})")


class EmailResult(BaseModel):
    email: Optional[str]
    score: Optional[int] = None  # Hunter confidence 0-100
    status: Optional[str] = None  # Hunter: valid / accept_all / unknown; Apollo: verified / likely to engage / ...
    domain: Optional[str] = None
    source: str  # hunter | apollo | pattern | manual
    error: Optional[str] = None
    found_at: datetime
    tried: list[str] = []  # lookup methods already attempted for this person


def tried_methods(entry: dict) -> list[str]:
    """Methods already attempted, including entries saved before `tried` existed."""
    if entry.get("tried"):
        return entry["tried"]
    return {"hunter": ["hunter"], "pattern": ["hunter", "pattern"], "apollo": ["hunter", "apollo"]}.get(
        entry.get("source"), [])


def clean_name(name: str) -> str:
    """'Ken(Yukun) Jiang' -> 'Ken Jiang'; 'David Golland, PhD' -> 'David Golland'."""
    name = re.sub(r"\(.*?\)", " ", name)
    name = name.split(",")[0]
    name = re.sub(r"[^\w\s'.-]", " ", name)  # emoji, symbols
    return re.sub(r"\s+", " ", name).strip()


def linkedin_handle(linkedin_url: str) -> str:
    return linkedin_url.rstrip("/").rsplit("/", 1)[-1]


async def find_email(name: str, linkedin_url: str, company: str, budget: HunterBudget,
                     domain: Optional[str] = None) -> EmailResult:
    try:
        await budget.spend(FIND_COST)  # worst case: Hunter only charges when it finds one
    except BudgetExceeded as e:
        return EmailResult(email=None, source="hunter", error=f"Hunter {e}", found_at=datetime.now())
    params = {
        "full_name": clean_name(name),
        "linkedin_handle": linkedin_handle(linkedin_url),
        "api_key": os.environ["HUNTER_API_KEY"],
    }
    if domain:
        params["domain"] = domain
    else:
        params["company"] = company
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(HUNTER_URL, params=params)
    now = datetime.now()
    body = response.json()
    if response.status_code != 200:
        details = "; ".join(e.get("details", "") for e in body.get("errors", [])) or response.reason_phrase
        # 404 = Hunter found nobody; anything else (quota, bad key) is an error worth surfacing.
        if response.status_code == 404:
            return EmailResult(email=None, source="hunter", error="No email found", found_at=now)
        return EmailResult(email=None, source="hunter", error=f"Hunter {response.status_code}: {details}", found_at=now)
    data = body["data"]
    return EmailResult(
        email=data.get("email"),
        score=data.get("score"),
        status=(data.get("verification") or {}).get("status"),
        domain=data.get("domain"),
        source="hunter",
        error=None if data.get("email") else "No email found",
        found_at=now,
    )


# Fallback when Hunter's finder has nobody: learn the company's address format
# from an email we already found there, guess, and keep a guess only if Hunter's
# verifier (1 verification credit each) says the mailbox exists.
VERIFY_URL = "https://api.hunter.io/v2/email-verifier"
PATTERNS = ["{first}.{last}", "{f}{last}", "{first}{last}", "{first}", "{first}_{last}", "{first}{l}", "{f}.{last}", "{last}{f}"]
MAX_GUESSES = 3


def _parts(name: str) -> tuple[str, str]:
    words = [re.sub(r"[^a-z]", "", w) for w in clean_name(name).lower().split()]
    words = [w for w in words if w]
    return (words[0], words[-1]) if len(words) >= 2 else (words[0] if words else "", "")


def _apply(pattern: str, first: str, last: str) -> str:
    return pattern.format(first=first, last=last, f=first[:1], l=last[:1])


def infer_pattern(name: str, email: str) -> Optional[str]:
    first, last = _parts(name)
    local = email.split("@")[0].lower()
    return next((p for p in PATTERNS if first and last and _apply(p, first, last) == local), None)


async def verify(email: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.get(VERIFY_URL, params={"email": email, "api_key": os.environ["HUNTER_API_KEY"]})
    body = response.json()
    if response.status_code != 200:
        details = "; ".join(e.get("details", "") for e in body.get("errors", [])) or response.reason_phrase
        raise RuntimeError(f"Hunter {response.status_code}: {details}")
    return body["data"]


async def guess_email(name: str, domain: str, known_patterns: list[str], budget: HunterBudget) -> EmailResult:
    """Try the company's known format first, then common ones; keep the first verified-valid guess."""
    first, last = _parts(name)
    now = datetime.now()
    if not (first and last):
        return EmailResult(email=None, source="pattern", error="Name too short to guess", found_at=now)
    order = list(dict.fromkeys(known_patterns + PATTERNS))[:MAX_GUESSES]
    tried = []
    for pattern in order:
        email = f"{_apply(pattern, first, last)}@{domain}"
        try:
            await budget.spend(VERIFY_COST)
            data = await verify(email)
        except BudgetExceeded as e:
            return EmailResult(email=None, source="pattern", error=f"Hunter {e}", found_at=now)
        except RuntimeError as e:
            return EmailResult(email=None, source="pattern", error=str(e), found_at=now)
        tried.append(email)
        if data.get("status") == "valid":
            return EmailResult(email=email, score=data.get("score"), status="valid", domain=domain,
                               source="pattern", found_at=now)
    return EmailResult(email=None, source="pattern", error=f"No email found (tried {', '.join(tried)})", found_at=now)


# Apollo people enrichment: POST /api/v1/people/match. Apollo bills 1 credit when
# it returns "credit-consuming data": an email OR demographic data for a matched
# person, so a match with no email can still cost 1. Nothing is charged when
# match_confidence is "none". Personal emails stay off (we want work addresses).
# Apollo's API can't report the credit balance (only its website can), so usage is
# counted locally the way Apollo bills it.
APOLLO_URL = "https://api.apollo.io/api/v1/people/match"
APOLLO_USAGE = project_path(".data/apollo_usage.json")
APOLLO_OK_CONFIDENCE = {"high", "medium"}


def hunter_enabled() -> bool:
    return bool(os.environ.get("HUNTER_API_KEY"))


def apollo_enabled() -> bool:
    return bool(os.environ.get("APOLLO_API_KEY"))


def apollo_cycle(today: Optional[date] = None) -> tuple[date, date]:
    """(start, next reset) of the current Apollo credit cycle. Apollo's API doesn't expose the
    renewal date, so it's `apollo.reset_day` (day of month, from the user's billing page)."""
    today = today or date.today()
    day = int(load_config().get("apollo", {}).get("reset_day", 1))

    def on(year, month):  # clamp e.g. day 31 to the month's last day
        return date(year, month, min(day, calendar.monthrange(year, month)[1]))

    start = on(today.year, today.month)
    if start > today:
        start = on(today.year - (today.month == 1), (today.month - 2) % 12 + 1)
    nxt = on(start.year + (start.month == 12), start.month % 12 + 1)
    return start, nxt


def apollo_used_this_cycle() -> int:
    try:
        return json.loads(APOLLO_USAGE.read_text()).get(apollo_cycle()[0].isoformat(), 0)
    except FileNotFoundError:
        return 0


def _record_apollo_credit() -> None:
    try:
        usage = json.loads(APOLLO_USAGE.read_text())
    except FileNotFoundError:
        usage = {}
    key = apollo_cycle()[0].isoformat()
    usage[key] = usage.get(key, 0) + 1
    APOLLO_USAGE.parent.mkdir(exist_ok=True)
    APOLLO_USAGE.write_text(json.dumps(usage, indent=2))


async def find_email_apollo(name: str, linkedin_url: str, company: str, limit: int,
                            domain: Optional[str] = None) -> EmailResult:
    now = datetime.now()
    if apollo_used_this_cycle() >= limit:
        return EmailResult(email=None, source="apollo", found_at=now,
                           error=f"Apollo limit reached: {limit} credits used this month")
    params = {"name": clean_name(name), "linkedin_url": linkedin_url.rstrip("/"),
              "organization_name": company, "reveal_personal_emails": "false"}
    if domain:
        params["domain"] = domain
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(APOLLO_URL, params=params,
                                   headers={"x-api-key": os.environ["APOLLO_API_KEY"], "Cache-Control": "no-cache"})
    if response.status_code != 200:
        try:
            details = response.json().get("error") or response.json().get("message") or response.text[:200]
        except ValueError:
            details = response.text[:200]
        return EmailResult(email=None, source="apollo", found_at=now,
                           error=f"Apollo {response.status_code}: {details}")
    person = response.json().get("person") or {}
    if person and (person.get("match_confidence") or "high") != "none":
        _record_apollo_credit()  # billed for the match itself, with or without an email
    email = person.get("email")
    if email and "email_not_unlocked" in email:  # placeholder Apollo returns for a locked email
        email = None
    confident = (person.get("match_confidence") or "high") in APOLLO_OK_CONFIDENCE
    if not email or not confident:
        return EmailResult(email=None, source="apollo", found_at=now, error="No email found")
    return EmailResult(
        email=email,
        status=person.get("email_status"),
        domain=email.split("@", 1)[1],
        source="apollo",
        found_at=now,
    )


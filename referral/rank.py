"""Rank alumni by how likely they are to be a good referrer for a posting.

Two passes:
  1. score_card() uses only the search card (headline + LinkedIn's summary),
     so every search result can be ranked without loading more pages.
  2. apply_profile() refines the score for the top few after their profile is
     scraped: confirms the school, gets the graduation year, checks they still
     work at the company.

Each score keeps its reasons so the review table can show why.
"""

import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from referral.config import school_short
from referral.profiles import Profile
from referral.search import SearchResult, company_queries

# Title tiers, checked in order; first match wins.
SENIORITY = [
    ("exec", r"\b(vp|vice president|director|head of|cto|chief)\b", -1),
    ("principal", r"\b(principal|distinguished|fellow)\b", 0),
    ("staff", r"\bstaff\b", 1),
    ("manager", r"\b(engineering manager|manager)\b", 2),
    ("senior", r"\b(senior|sr\.?|lead)\b", 2),
    ("junior", r"\b(software engineer|swe|developer|engineer)\b", 3),
]
RECRUITER_RE = re.compile(r"\b(recruit\w*|talent|sourcer)\b", re.I)
def card_year_re(school: str) -> re.Pattern:
    """For Cornell University: matches "Cornell'22", "Cornell ’22", "Cornell 2022", "Cornell University '19"."""
    key = re.escape(school_short(school))
    return re.compile(rf"{key}(?:\s+university)?\s*[’'`]?\s*(\d{{2}}|\d{{4}})\b", re.I)


class Candidate(BaseModel):
    result: SearchResult
    score: int = 0
    reasons: list[str] = Field(default_factory=list)
    grad_year: Optional[int] = None
    seniority: Optional[str] = None
    verified: Optional[bool] = None  # None = profile not scraped
    degree: Optional[str] = None  # best degree at the school: undergrad | masters | phd | unknown | certificate
    profile: Optional[Profile] = None


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def recency_points(grad_year: int, today: Optional[date] = None) -> tuple[int, str]:
    years = (today or date.today()).year - grad_year
    if years < 0:
        return 1, f"current student ('{grad_year % 100:02d})"
    if years <= 3:
        return 3, f"recent grad ('{grad_year % 100:02d})"
    if years <= 7:
        return 2, f"grad '{grad_year % 100:02d}"
    if years <= 12:
        return 1, f"grad '{grad_year % 100:02d}"
    return 0, f"grad '{grad_year % 100:02d}"


def score_card(result: SearchResult, target_roles: list[str], team: Optional[str], school: str) -> Candidate:
    c = Candidate(result=result)
    headline = (result.headline or "").lower()
    text = f"{headline} {(result.summary or '').lower()}"

    def add(points: int, reason: str):
        c.score += points
        c.reasons.append(f"{points:+d} {reason}")

    if any(role.lower() in text for role in target_roles):
        add(3, "role match")

    if RECRUITER_RE.search(headline):
        c.seniority = "recruiter"
        add(-1, "recruiter")
    else:
        # The summary's first clause is usually the current title, more reliable than a headline tagline.
        current_title = (result.summary or "").split(" at ")[0].lower() or headline
        for tier, pattern, points in SENIORITY:
            if re.search(pattern, current_title) or re.search(pattern, headline):
                c.seniority = tier
                add(points, tier)
                break

    if m := card_year_re(school).search(f"{result.headline or ''} {result.summary or ''}"):
        c.grad_year = _year(m.group(1))
        add(*recency_points(c.grad_year))

    if team and team.lower() in text:
        add(2, f"on {team}")

    if result.degree in ("1st", "2nd"):
        add(2 if result.degree == "1st" else 1, f"{result.degree} connection")
    return c


def company_matches(posting_company: str, profile_company: str) -> bool:
    """'Zillow Group' (posting) vs 'Zillow' (LinkedIn): either name containing the other counts."""
    theirs = profile_company.strip().lower()
    if not theirs:
        return False
    return any(n in theirs or theirs in n for n in (q.lower() for q in company_queries(posting_company)))


# Degree levels at your school, best first, with their points. Undergrad alumni are the
# strongest connection; a certificate (or summer/online program) is barely one, so it's
# penalized almost as much as not having the school at all.
DEGREE_POINTS = {"undergrad": 3, "masters": 1, "phd": 0, "unknown": 0, "certificate": -8}
DEGREE_LABELS = {"undergrad": "undergrad", "masters": "master's", "phd": "PhD", "unknown": "degree not listed",
                 "certificate": "certificate only"}
_CERTIFICATE_RE = re.compile(r"certific|executive ed|summer|exchange|visiting|non-?degree|online|nanodegree|"
                             r"\becornell\b", re.I)
_PHD_RE = re.compile(r"\bph\.?\s?d\b|doctor", re.I)
_MASTERS_RE = re.compile(r"master|\bm\.?\s?(s|eng|a|sc|ps|pa|ph|fa|ba)\b\.?|\bmeng\b|\bmba\b|\bmps\b|\bllm\b", re.I)
_UNDERGRAD_RE = re.compile(r"bachelor|undergrad|\bb\.?\s?(s|a|sc|e|eng|fa|se)\b\.?|\ba\.?b\.?(?=\s|$)|\bbs\b|\bba\b", re.I)


def degree_level(education) -> str:
    text = f"{education.degree or ''} | {education.field_of_study or ''}"
    if _CERTIFICATE_RE.search(f"{text} | {education.school}"):
        return "certificate"
    if _PHD_RE.search(text):
        return "phd"
    if _MASTERS_RE.search(text):
        return "masters"
    if _UNDERGRAD_RE.search(text):
        return "undergrad"
    return "unknown"


def apply_profile(c: Candidate, profile: Profile, school: str, company: str) -> Candidate:
    """Refine a card score with the scraped profile."""
    c.profile = profile
    key = school_short(school).lower()
    schools = [e for e in profile.educations if key in e.school.lower()]
    c.verified = bool(schools)
    if not schools:
        c.score -= 10
        c.reasons.append(f"-10 no {school} on profile")
        return c

    levels = {id(e): degree_level(e) for e in schools}
    c.degree = min(levels.values(), key=list(DEGREE_POINTS).index)  # best level wins
    points = DEGREE_POINTS[c.degree]
    if points:
        c.score += points
        c.reasons.append(f"{points:+d} {DEGREE_LABELS[c.degree]}")

    # Class year from real degrees, not from a certificate taken later.
    degrees = [e for e in schools if levels[id(e)] != "certificate"] or schools
    years = [e.end_year for e in degrees if e.end_year]
    if years and c.grad_year is None:
        c.grad_year = max(years)
        c.reasons.append("{:+d} {}".format(*recency_points(c.grad_year)))
        c.score += recency_points(c.grad_year)[0]

    current = [x for x in profile.experiences if x.is_current and company_matches(company, x.company)]
    if not current:
        c.score -= 5
        c.reasons.append(f"-5 no current {company} role on profile")
    return c


def rank(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def rescore(candidates: list[dict], target_roles: list[str], school: str, company: str, team: Optional[str]) -> list[Candidate]:
    """Recompute scores for a saved run from its stored cards and profiles (no scraping)."""
    out = []
    for saved in candidates:
        c = score_card(SearchResult(**saved["result"]), target_roles, team, school)
        if saved.get("profile"):
            apply_profile(c, Profile(**saved["profile"]), school, company)
        out.append(c)
    return rank(out)

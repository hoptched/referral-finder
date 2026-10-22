"""The find flow shared by the CLI and the web app.

find_candidates(): resolve company → search alumni → rank → scrape the top
few profiles (within the run/day limits) → re-rank. Progress goes to `log`.
"""

import asyncio
import random
from datetime import datetime
from typing import Callable, Optional

from referral.config import project_path
from referral.jobs import JobPosting
from referral.linkedin import open_session
from referral.profiles import scrape_profile
from referral.rank import Candidate, apply_profile, rank, score_card
from referral.search import resolve_company, search_alumni
from referral.store import cached_profile, record_scrape, run_path, save_profile, save_run, scrapes_today

Log = Callable[[str], None]


async def find_candidates(
    cfg: dict,
    posting: JobPosting,
    company_url: Optional[str] = None,
    keywords: Optional[str] = None,
    pages: Optional[int] = None,
    verify: Optional[int] = None,
    log: Log = print,
) -> list[Candidate]:
    li = cfg["linkedin"]
    keywords = keywords or cfg["target_roles"][0]
    pages = pages or li["search_pages"]
    verify = li["profiles_to_verify"] if verify is None else verify

    async with open_session(project_path(li["session_path"]), li["headless"]) as browser:
        log(f"Finding {posting.company} on LinkedIn…")
        company = await resolve_company(browser.page, posting.company, company_url)
        log(f"Using LinkedIn company page {company.linkedin_url} (wrong? paste the right one in “LinkedIn company URL” and search again)")
        log(f"Searching {cfg['me']['school']} alumni at {posting.company} for “{keywords}”…")
        results = await search_alumni(browser.page, company, cfg["me"]["linkedin_school_id"], keywords, pages)
        log(f"Found {len(results)} alumni. Ranking…")
        candidates = rank([score_card(r, cfg["target_roles"], posting.team, cfg["me"]["school"]) for r in results])

        budget = min(verify, li["max_profiles_per_run"], li["max_profiles_per_day"] - scrapes_today())
        if budget < verify:
            log(f"Scrape limit: verifying {max(budget, 0)} of {verify} profiles.")
        scraped = 0
        for c in candidates[:verify]:
            profile = cached_profile(c.result.linkedin_url)
            if profile is None:
                if scraped >= budget:
                    continue
                if scraped:
                    await asyncio.sleep(random.uniform(*li["delay_seconds"]))
                log(f"Checking {c.result.name}'s profile…")
                profile = await scrape_profile(browser.page, c.result.linkedin_url, cfg["models"]["extract"])
                record_scrape()
                save_profile(profile)
                scraped += 1
            apply_profile(c, profile, cfg["me"]["school"], posting.company)
    return rank(candidates)


def write_run(posting: JobPosting, candidates: list[Candidate], selected: list[str]):
    path = run_path(posting.company, posting.url)
    save_run(path, {
        "created_at": datetime.now(),
        "job": posting.model_dump(),
        "candidates": [c.model_dump() for c in candidates],
        "selected": selected,
    })
    return path

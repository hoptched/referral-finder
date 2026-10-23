import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from referral import pipeline
from referral.config import load_config
from referral.jobs import JobPosting
from referral.search import Company, SearchResult


def _person(slug, headline):
    return SearchResult(name=slug.title(), linkedin_url=f"https://www.linkedin.com/in/{slug}/", degree="2nd",
                        headline=headline, location=None, summary=None)


def test_few_keyword_matches_falls_back_to_all_alumni(monkeypatch):
    """Real case: American Family Insurance had 4 Cornell alumni and none matched "software engineer"."""
    searches = []

    @asynccontextmanager
    async def session(path, headless):
        yield SimpleNamespace(page=None)

    async def resolve(page, name, url, model):
        return Company(name=name, linkedin_url="https://www.linkedin.com/company/x/", ids=["1"])

    async def search(page, company, school, keywords, pages):
        searches.append(keywords)
        if keywords:
            return [_person("a", "Software Engineer at X")]
        return [_person("a", "Software Engineer at X"), _person("b", "Data & Analytics Leader")]

    monkeypatch.setattr(pipeline, "open_session", session)
    monkeypatch.setattr(pipeline, "resolve_company", resolve)
    monkeypatch.setattr(pipeline, "search_alumni", search)
    posting = JobPosting(url="https://x.com/1", source="fake", company="X", title="BI Intern")
    cands = asyncio.run(pipeline.find_candidates(load_config(), posting, verify=0, log=lambda line: None))
    assert searches == ["software engineer", None]
    assert sorted(c.result.name for c in cands) == ["A", "B"]  # no duplicate of A

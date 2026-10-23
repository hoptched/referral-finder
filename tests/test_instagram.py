import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from referral import instagram, simplify, store, web
from referral.config import load_config
from referral.instagram import Story, StoryJob, StoryJobs


def test_story_links_from_stickers():
    item = {
        "pk": "1",
        "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/x.jpg", "width": 1080}]},
        "story_link_stickers": [{"story_link": {
            "url": "https://l.instagram.com/?u=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F1&e=abc",
            "display_url": "boards.greenhouse.io/acme/jobs/1"}}],
        "story_cta": [{"links": [{"webUri": "https://bit.ly/xyz"}]}],
        "caption": {"text": "https://not-a-sticker.com"},
    }
    assert instagram.story_links(item) == ["https://boards.greenhouse.io/acme/jobs/1", "https://bit.ly/xyz"]


def test_image_url_picks_readable_size():
    item = {"image_versions2": {"candidates": [{"url": "big", "width": 1440}, {"url": "mid", "width": 750},
                                               {"url": "tiny", "width": 320}]}}
    assert instagram._image_url(item) == "mid"


@pytest.fixture
def ig_state(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram, "STATE_PATH", tmp_path / "instagram.json")
    monkeypatch.setattr(instagram, "RAW_DIR", tmp_path / "raw")
    return tmp_path


def _fake_claude(monkeypatch, jobs):
    async def parse(**kwargs):
        return SimpleNamespace(parsed_output=StoryJobs(jobs=jobs), model="claude-haiku-5-5",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(instagram, "client", lambda: SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    monkeypatch.setattr(instagram, "log_usage", lambda *a: None)

    async def resolve(url):
        return {"https://bit.ly/a": "https://acme.wd1.myworkdayjobs.com/job/1?utm_source=ig"}.get(url, url)

    monkeypatch.setattr(instagram, "_resolve", resolve)


def test_read_story_only_keeps_links_on_the_story(monkeypatch):
    _fake_claude(monkeypatch, [
        StoryJob(company="Acme", title="SWE Intern", link="https://acme.wd1.myworkdayjobs.com/job/1?utm_source=ig",
                 locations=["NYC"], phd_only=False),
        StoryJob(company="Made Up", title="Intern", link="https://invented.example.com", locations=[], phd_only=False),
        StoryJob(company="No Link", title="Intern", link=None, locations=[], phd_only=False),
    ])
    story = Story(id="9", account="zero2sudo", taken_at=100, links=["https://bit.ly/a"], image=b"jpg")
    jobs = asyncio.run(instagram.read_story(story, "claude-haiku-5-5"))
    assert [(j["company"], j["url"], j["source"]) for j in jobs] == [
        ("Acme", "https://acme.wd1.myworkdayjobs.com/job/1", "zero2sudo")]


def test_check_reads_each_story_once(monkeypatch, ig_state):
    now = int(time.time())
    stories = [Story(id="1", account="zero2sudo", taken_at=now, links=["https://bit.ly/a"], image=b"x"),
               Story(id="2", account="zero2sudo", taken_at=now, links=[], image=b"x")]
    read = []

    async def fetch(path, account):
        return stories

    async def read_story(story, model):
        read.append(story.id)
        return [{"id": f"ig-{story.id}-0", "company": "Acme", "title": "SWE Intern", "url": "https://acme.com/1",
                 "locations": [], "category": None, "posted": story.taken_at, "source": "zero2sudo", "phd_only": False}]

    monkeypatch.setattr(instagram, "fetch_stories", fetch)
    monkeypatch.setattr(instagram, "read_story", read_story)
    cfg = load_config()
    state = asyncio.run(instagram.check(cfg, log=lambda line: None))
    assert read == ["1", "2"] and len(state["jobs"]) == 1  # same link from two stories: listed once
    assert state["error"] is None and not instagram.due(cfg)
    asyncio.run(instagram.check(cfg, log=lambda line: None))
    assert read == ["1", "2"]  # already seen: no second Claude call


def test_check_records_login_problem(monkeypatch, ig_state):
    async def fetch(path, account):
        raise instagram.InstagramLoginNeeded("Instagram session expired. Run: refer ig-login")

    monkeypatch.setattr(instagram, "fetch_stories", fetch)
    cfg = load_config()
    state = asyncio.run(instagram.check(cfg, log=lambda line: None))
    assert state["error"] == "Instagram session expired. Run: refer ig-login"
    assert not instagram.due(cfg)  # waits 6 hours before retrying
    state["last_run"] = (datetime.now() - timedelta(hours=7)).isoformat()
    assert instagram.due(cfg, state)


def test_jobs_endpoint_merges_story_jobs(monkeypatch, ig_state, tmp_path):
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "_queue_items", [])
    now = time.time()
    listed = {"id": "s1", "company": "Acme", "title": "SWE Intern", "url": "https://acme.com/1?utm_source=Simplify",
              "locations": ["NYC"], "category": "Software", "posted": now - 50, "source": "Simplify"}

    async def listings(cfg, refresh=False):
        return {"fetched_at": now, "jobs": [listed]}

    monkeypatch.setattr(simplify, "listings", listings)
    story_job = lambda i, url, **kw: {"id": f"ig-{i}", "company": "B", "title": "Intern", "url": url, "locations": [],
                                      "category": None, "posted": now, "source": "zero2sudo", "phd_only": False} | kw
    instagram.save_state({"jobs": [story_job(1, "https://acme.com/1"), story_job(2, "https://b.com/2"),
                                   story_job(3, "https://c.com/3", phd_only=True),
                                   story_job(4, "https://d.com/4", locations=["London, UK"]),
                                   story_job(5, "https://e.com/5", posted=now - 30 * 86400)],
                          "last_success": "2026-10-22T09:00:00", "error": None, "seen": {}})
    out = TestClient(web.app).get("/api/jobs").json()
    assert [(j["id"], j.get("also")) for j in out["jobs"]] == [("ig-2", None), ("s1", ["zero2sudo"])]
    assert out["instagram"]["last_success"] == "2026-10-22T09:00:00"

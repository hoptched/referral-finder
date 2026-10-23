import json

import pytest
from fastapi.testclient import TestClient

from referral import store, web

RUN = {
    "created_at": "2026-10-21T17:48:16",
    "job": {"url": "https://jobs.example.com/1", "source": "greenhouse", "company": "Acme", "title": "SWE Intern",
            "department": None, "team": None, "location": "NYC", "description": ""},
    "candidates": [{"result": {"name": "A", "linkedin_url": "https://www.linkedin.com/in/a/", "degree": "3rd+",
                               "headline": "SWE", "location": None, "summary": None},
                    "score": 5, "reasons": ["+3 role match"], "grad_year": 2024, "seniority": "junior",
                    "verified": True, "profile": None}],
    "selected": [],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RUNS_DIR", tmp_path)
    (tmp_path / "acme-1.json").write_text(json.dumps(RUN))
    return TestClient(web.app)


def test_index_and_runs(client):
    assert "Referral Finder" in client.get("/").text
    runs = client.get("/api/runs").json()
    assert runs == [{"id": "acme-1", "company": "Acme", "title": "SWE Intern",
                     "created_at": "2026-10-21T17:48:16", "candidates": 1, "selected": 0, "applied": False, "url": RUN["job"]["url"],
                     "outreach": {"drafted": 0, "invites": 0, "sent": 0, "done": False}}]


def test_select_keeps_only_known_people(client, tmp_path):
    r = client.put("/api/runs/acme-1/selected",
                   json={"selected": ["https://www.linkedin.com/in/a/", "https://www.linkedin.com/in/stranger/"]})
    assert r.json() == {"selected": ["https://www.linkedin.com/in/a/"]}
    assert json.loads((tmp_path / "acme-1.json").read_text())["selected"] == ["https://www.linkedin.com/in/a/"]


def test_bad_run_ids(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404


def test_email_lookup_skips_known_and_reuses_domain(client, monkeypatch):
    from referral.emails import EmailResult

    calls = []

    async def fake_find(name, url, company, budget, domain=None):
        calls.append((name, domain))
        return EmailResult(email="a@acme.com", score=90, status="valid", domain="acme.com", source="hunter",
                           found_at="2026-10-21T00:00:00")

    monkeypatch.setattr(web, "find_email", fake_find)
    client.put("/api/runs/acme-1/selected", json={"selected": ["https://www.linkedin.com/in/a/"]})
    emails = client.post("/api/runs/acme-1/emails").json()["emails"]
    assert emails["https://www.linkedin.com/in/a/"]["email"] == "a@acme.com"
    client.post("/api/runs/acme-1/emails")  # already looked up: no second Hunter call
    assert calls == [("A", None)]


def test_manual_email_and_draft_edit(client, monkeypatch):
    from referral.compose import Draft

    async def fake_compose(cfg, job, candidate, to):
        return Draft(to=to, subject="Hi", body="Body", invitation="Hi!", short_title="SWE Intern", company="Acme",
                     generated_at="2026-10-21T00:00:00")

    monkeypatch.setattr(web, "compose", fake_compose)
    monkeypatch.setattr(web, "load_config", lambda: {})
    url = "https://www.linkedin.com/in/a/"
    client.put("/api/runs/acme-1/selected", json={"selected": [url]})
    client.put("/api/runs/acme-1/emails", json={"linkedin_url": url, "email": "me@acme.com"})
    drafts = client.post("/api/runs/acme-1/drafts", json={}).json()["drafts"]
    assert drafts[url]["to"] == "me@acme.com" and not drafts[url]["edited"]
    drafts = client.put("/api/runs/acme-1/drafts", json={"linkedin_url": url, "subject": "S", "body": "B", "invitation": "Hi!"}).json()["drafts"]
    assert drafts[url] | {"generated_at": None} == {"to": "me@acme.com", "subject": "S", "body": "B",
                                                    "invitation": "Hi!", "short_title": "SWE Intern",
                                                    "company": "Acme", "generated_at": None, "edited": True}
    # Toggling selection afterwards must not lose the draft.
    client.put("/api/runs/acme-1/selected", json={"selected": []})
    assert client.get("/api/runs/acme-1").json()["drafts"][url]["subject"] == "S"


def test_send_once_then_blocked(client, monkeypatch, tmp_path):
    from referral import gmail
    from referral.compose import Draft

    async def fake_compose(cfg, job, candidate, to):
        return Draft(to=to, subject="Hi", body="Body", invitation="Hi!", short_title="SWE Intern", company="Acme",
                     generated_at="2026-10-21T00:00:00")

    sent = []
    monkeypatch.setattr(web, "compose", fake_compose)
    monkeypatch.setattr(store, "SENT_LOG", tmp_path / "sent.jsonl")
    monkeypatch.setattr(gmail, "send", lambda message: sent.append(message) or "gmail-id-1")
    monkeypatch.setattr(gmail, "account_email", lambda: "me@gmail.com")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(web, "load_config", lambda: {"me": {"name": "Alex Doe", "resume_path": str(resume)}})
    monkeypatch.setattr(web, "project_path", lambda p: tmp_path / p)

    url = "https://www.linkedin.com/in/a/"
    client.put("/api/runs/acme-1/selected", json={"selected": [url]})
    assert client.post("/api/runs/acme-1/drafts", json={}).json()["drafts"] == {}  # never drafted without an email
    assert client.post("/api/runs/acme-1/drafts", json={"linkedin_url": url}).status_code == 400
    assert client.post("/api/runs/acme-1/send", json={"linkedin_url": url}).status_code == 400  # no draft

    client.put("/api/runs/acme-1/emails", json={"linkedin_url": url, "email": "a@acme.com"})
    client.post("/api/runs/acme-1/drafts", json={})
    test = client.post("/api/runs/acme-1/send", json={"linkedin_url": url, "test": True}).json()["sent"]
    assert test["to"] == "me@gmail.com" and sent[-1]["Subject"] == "[TEST] Hi"
    assert not (tmp_path / "sent.jsonl").exists()  # tests aren't logged

    r = client.post("/api/runs/acme-1/send", json={"linkedin_url": url})
    assert r.status_code == 200 and r.json()["run_sent"][url]["to"] == "a@acme.com"
    attachment = next(sent[-1].iter_attachments())
    assert attachment.get_filename() == "Alex_Doe_Resume.pdf"
    assert client.post("/api/runs/acme-1/send", json={"linkedin_url": url}).status_code == 409  # never twice
    assert len(sent) == 2


def _two_person_run(tmp_path):
    run = json.loads(json.dumps(RUN))
    run["candidates"].append({**run["candidates"][0], "result": {**run["candidates"][0]["result"],
                              "name": "B", "linkedin_url": "https://www.linkedin.com/in/b/"}})
    run["selected"] = ["https://www.linkedin.com/in/a/", "https://www.linkedin.com/in/b/"]
    # A: saved before Apollo existed: Hunter missed, guesses failed. B: never looked up.
    run["emails"] = {"https://www.linkedin.com/in/a/": {"email": None, "source": "pattern", "error": "No email found (tried x)",
                                                        "found_at": "2026-10-21T00:00:00"}}
    (tmp_path / "acme-1.json").write_text(json.dumps(run))


def test_apollo_after_hunter_and_for_old_misses(client, monkeypatch, tmp_path):
    from referral.emails import EmailResult

    _two_person_run(tmp_path)
    monkeypatch.setenv("APOLLO_API_KEY", "test")
    calls = []

    async def hunter(name, url, company, budget, domain=None):
        calls.append(("hunter", name))
        return EmailResult(email=None, source="hunter", error="No email found", found_at="2026-10-21T00:00:00")

    async def apollo(name, url, company, limit, domain=None):
        calls.append(("apollo", name))
        return EmailResult(email=f"{name.lower()}@acme.com", status="verified", domain="acme.com", source="apollo",
                           found_at="2026-10-21T00:00:00")

    monkeypatch.setattr(web, "find_email", hunter)
    monkeypatch.setattr(web, "find_email_apollo", apollo)
    emails = client.post("/api/runs/acme-1/emails").json()["emails"]
    assert calls == [("hunter", "B"), ("apollo", "A"), ("apollo", "B")]  # A isn't sent to Hunter again
    assert emails["https://www.linkedin.com/in/a/"]["email"] == "a@acme.com"
    assert emails["https://www.linkedin.com/in/b/"]["tried"] == ["apollo", "hunter"]
    client.post("/api/runs/acme-1/emails")
    assert len(calls) == 3  # everyone has an email now: nothing more to try


def test_apollo_quota_error_stops_apollo(client, monkeypatch, tmp_path):
    from referral.emails import EmailResult

    _two_person_run(tmp_path)
    monkeypatch.setenv("APOLLO_API_KEY", "test")
    calls = []

    async def hunter(name, url, company, budget, domain=None):
        return EmailResult(email=None, source="hunter", error="No email found", found_at="2026-10-21T00:00:00")

    async def apollo(name, url, company, limit, domain=None):
        calls.append(name)
        return EmailResult(email=None, source="apollo", error="Apollo 422: insufficient credits",
                           found_at="2026-10-21T00:00:00")

    async def guess(name, domain, patterns, budget):
        raise AssertionError("no domain known, so no guessing")

    monkeypatch.setattr(web, "find_email", hunter)
    monkeypatch.setattr(web, "find_email_apollo", apollo)
    monkeypatch.setattr(web, "guess_email", guess)
    client.post("/api/runs/acme-1/emails")
    assert calls == ["A"]  # stopped after the first quota error


def test_apollo_extrapolated_email_is_kept_unchecked(client, monkeypatch, tmp_path):
    from referral import emails as emails_mod
    from referral.emails import EmailResult

    _two_person_run(tmp_path)
    monkeypatch.setenv("APOLLO_API_KEY", "test")

    async def hunter(name, url, company, budget, domain=None):
        return EmailResult(email=None, source="hunter", error="No email found", found_at="2026-10-21T00:00:00")

    async def apollo(name, url, company, limit, domain=None):
        return EmailResult(email=f"{name.lower()}@acme.com", status="extrapolated", domain="acme.com",
                           source="apollo", found_at="2026-10-21T00:00:00")

    async def no_verify(email):
        raise AssertionError("Hunter's verifier must not be used for Apollo results")

    monkeypatch.setattr(web, "find_email", hunter)
    monkeypatch.setattr(web, "find_email_apollo", apollo)
    monkeypatch.setattr(emails_mod, "verify", no_verify)
    result = client.post("/api/runs/acme-1/emails").json()["emails"]
    b = result["https://www.linkedin.com/in/b/"]
    assert (b["email"], b["status"]) == ("b@acme.com", "extrapolated")


def test_outreach_drafts_only_people_with_email(client, monkeypatch, tmp_path):
    from referral.compose import Draft, JobNames
    from referral.emails import EmailResult

    _two_person_run(tmp_path)
    run = json.loads((tmp_path / "acme-1.json").read_text())
    run["emails"] = {}
    (tmp_path / "acme-1.json").write_text(json.dumps(run))
    drafted = []

    async def hunter(name, url, company, budget, domain=None):
        found = name == "A"
        return EmailResult(email="a@acme.com" if found else None, source="hunter",
                           error=None if found else "No email found", found_at="2026-10-21T00:00:00")

    async def compose(cfg, job, candidate, to):
        drafted.append(candidate["result"]["name"])
        return Draft(to=to, subject="S", body="B", invitation="I", short_title="SWE Intern", company="Acme",
                     generated_at="2026-10-21T00:00:00")

    async def names(job, model):
        return JobNames(short_title="SWE Intern", company="Acme")

    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    monkeypatch.setattr(web, "find_email", hunter)
    monkeypatch.setattr(web, "compose", compose)
    monkeypatch.setattr(web, "job_names", names)
    out = client.post("/api/runs/acme-1/outreach").json()
    assert out["errors"] == [] and drafted == ["A"]
    run = out["run"]
    assert list(run["drafts"]) == ["https://www.linkedin.com/in/a/"]
    invite = run["invitations"]["https://www.linkedin.com/in/b/"]
    assert invite.startswith("Hi B!") and "SWE Intern role at Acme" in invite
    assert "https://www.linkedin.com/in/a/" not in run["invitations"]

    client.post("/api/runs/acme-1/outreach")  # second open: nothing left to do
    assert drafted == ["A"]


def test_outreach_requires_selection(client):
    assert client.post("/api/runs/acme-1/outreach").status_code == 400


def test_credits_panel(client, monkeypatch):
    from referral import emails as emails_mod
    from referral import gmail

    async def down(self):
        raise RuntimeError("hunter down")

    monkeypatch.setattr(emails_mod.HunterBudget, "status", down)
    monkeypatch.setattr(gmail, "status", lambda: {"connected": True, "account": "me@x.com", "error": None})
    monkeypatch.setattr(store, "scrapes_today", lambda: 12)
    c = client.get("/api/credits").json()
    assert "hunter down" in c["hunter"]["error"]  # one source failing doesn't break the panel
    assert c["linkedin"]["left"] == c["linkedin"]["limit"] - 12
    assert c["gmail"]["account"] == "me@x.com" and "usd_month" in c["claude"] and "left" in c["apollo"]


def test_runs_list_outreach_status(client, tmp_path):
    run = json.loads((tmp_path / "acme-1.json").read_text())
    url = "https://www.linkedin.com/in/a/"
    assert client.get("/api/runs").json()[0]["outreach"]["done"] is False  # nothing selected
    run["selected"] = [url]
    run["invitations"] = {url: "Hi A!"}
    run["sent"] = {}
    (tmp_path / "acme-1.json").write_text(json.dumps(run))
    assert client.get("/api/runs").json()[0]["outreach"] == {"drafted": 0, "invites": 1, "sent": 0, "done": True}


def test_applied_checkbox(client, tmp_path):
    assert client.get("/api/runs").json()[0]["applied"] is False
    assert client.put("/api/runs/acme-1/applied", json={"applied": True}).json() == {"applied": True}
    assert client.get("/api/runs").json()[0]["applied"] is True
    assert json.loads((tmp_path / "acme-1.json").read_text())["applied"] is True
    assert client.put("/api/runs/nope/applied", json={"applied": True}).status_code == 404

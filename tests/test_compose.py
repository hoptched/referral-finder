from referral.compose import _fill, first_name, load_template
from referral.emails import clean_name


def test_template_sections_and_comment_stripped():
    t = load_template()
    assert "{short_title}" in t.subject
    assert "{blurb}" in t.body and "{job_link}" in t.body and "{application_email}" in t.body
    assert "{first_name}" in t.invitation
    assert "Placeholders:" not in t.instructions  # lives in the stripped <!-- --> comment


def test_fill_leaves_unknown_braces():
    assert _fill("{a} {b} {}", {"a": 1}) == "1 {b} {}"


def test_names():
    assert first_name("Ken(Yukun) Jiang") == "Ken"
    assert clean_name("Ken(Yukun) Jiang") == "Ken Jiang"
    assert clean_name("David Golland, PhD") == "David Golland"


def test_email_patterns():
    from referral.emails import _apply, _parts, infer_pattern

    assert infer_pattern("Dana Atkins", "datkins@example.com") == "{f}{last}"
    assert infer_pattern("Rita Jones", "rita.jones@x.com") == "{first}.{last}"
    assert infer_pattern("Rita Jones", "rj1234@x.com") is None
    assert _parts("Ken(Yukun) Jiang") == ("ken", "jiang")
    assert _apply("{first}{l}", "sam", "sample") == "sams"


def test_invitation_fits_linkedin_limit():
    # Worst realistic fill: long names. Free accounts allow 200 characters.
    t = load_template()
    note = _fill(t.invitation, {"first_name": "Christopher", "class_year_short": "28", "school_short": "Cornell",
                                "short_title": "Software Engineer Intern", "company": "LinkedIn"})
    assert len(note) <= 200, len(note)


def test_hunter_budget_blocks_overspend(monkeypatch):
    import asyncio

    import pytest

    from referral.emails import BudgetExceeded, HunterBudget

    budget = HunterBudget(limit=50)

    async def status():
        return {"used": 49.5, "limit": 50, "left": 0.5, "resets": "2026-11-21"}

    monkeypatch.setattr(budget, "status", status)
    asyncio.run(budget.spend(0.5))  # a verification still fits
    with pytest.raises(BudgetExceeded):
        asyncio.run(budget.spend(1.0))  # a search would go over


def test_find_email_apollo_parsing(monkeypatch, tmp_path):
    import asyncio

    import httpx

    from referral import emails

    monkeypatch.setenv("APOLLO_API_KEY", "test")
    monkeypatch.setattr(emails, "APOLLO_USAGE", tmp_path / "apollo.json")
    replies = []
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=replies.pop(0))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(emails.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    find = lambda limit=10: asyncio.run(
        emails.find_email_apollo("Sam Sample", "https://www.linkedin.com/in/sam-sample/", "Acme Group", limit))

    replies.append({"person": {"email": "ssample@acme.com", "email_status": "verified", "match_confidence": "high"}})
    r = find()
    assert (r.email, r.status, r.domain, r.source) == ("ssample@acme.com", "verified", "acme.com", "apollo")
    assert seen[0].headers["x-api-key"] == "test"
    assert seen[0].url.params["reveal_personal_emails"] == "false"
    assert emails.apollo_used_this_cycle() == 1

    replies.append({"person": {"email": "x@acme.com", "match_confidence": "low"}})
    assert find().email is None  # low-confidence match: probably the wrong person

    assert emails.apollo_used_this_cycle() == 2  # Apollo still charged for the low-confidence email

    replies.append({"person": {"email": "email_not_unlocked@domain.com", "match_confidence": "high"}})
    assert find().email is None and emails.apollo_used_this_cycle() == 3  # matched person: billed even without email

    replies.append({"person": {"match_confidence": "none"}})
    assert find().email is None and emails.apollo_used_this_cycle() == 3  # no match: free

    replies.append({"person": None})
    assert find().email is None and emails.apollo_used_this_cycle() == 3

    r = find(limit=3)  # limit reached: no request made
    assert r.error.startswith("Apollo limit reached") and len(seen) == 5


def test_apollo_cycle(monkeypatch):
    from datetime import date

    from referral import emails

    monkeypatch.setattr(emails, "load_config", lambda: {"apollo": {"reset_day": 15}})
    assert emails.apollo_cycle(date(2026, 10, 22)) == (date(2026, 10, 15), date(2026, 11, 15))
    assert emails.apollo_cycle(date(2026, 10, 3)) == (date(2026, 9, 15), date(2026, 10, 15))
    assert emails.apollo_cycle(date(2026, 1, 3)) == (date(2025, 12, 15), date(2026, 1, 15))
    monkeypatch.setattr(emails, "load_config", lambda: {"apollo": {"reset_day": 31}})
    assert emails.apollo_cycle(date(2026, 2, 10)) == (date(2026, 1, 31), date(2026, 2, 28))

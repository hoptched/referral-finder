import asyncio

from playwright.async_api import async_playwright

from referral.search import CARDS_JS, _compact, _parse_card

# Mirrors LinkedIn's search-result markup closely enough: obfuscated divs, a
# name link, "• 2nd" degree marker, and a mutual-connection link to another profile.
PAGE = """<main><div>
  <div><div><a href="/in/alice-a/"><span>Alice A</span></a></div><p>• 2nd</p>
    <p>Software Engineer at LinkedIn</p><p>Sunnyvale, California</p><button>Connect</button>
    <p>Software Engineer at LinkedIn since 2023, working on feed ranking. You both went to Cornell University.</p>
    <p><a href="/in/bob-mutual/">Bob Mutual</a> is a mutual connection</p></div>
  <div><div><a href="/in/carol-c/?miniProfileUrn=x"><span>Carol C</span></a> • 3rd+</div>
    <p>SWE @ LinkedIn | CS @ Cornell'22</p><p>New York</p><button>Message</button></div>
  <div><a href="/in/dan-d/"><div>Dan D</div><div>• 3rd+</div><div>Senior Software Engineer @ LinkedIn</div>
    <div>New York City Metropolitan Area</div><div>Follow</div>
    <div>Senior Software Engineer at LinkedIn since March 2022, full-stack engineer on Premium.</div>
    <div>2,475 followers</div><div>Book an appointment</div></a></div>
</div></main>"""


def cards():
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(PAGE)
            result = await page.evaluate(CARDS_JS)
            await browser.close()
            return result

    return asyncio.run(run())


def test_cards_skip_mutual_connection_links():
    found = cards()
    assert [c["slug"] for c in found] == ["alice-a", "carol-c", "dan-d"]
    assert [c["name"] for c in found] == ["Alice A", "Carol C", "Dan D"]


def test_parse_card_fields():
    alice, carol, dan = (_parse_card(**c) for c in cards())
    assert alice.linkedin_url == "https://www.linkedin.com/in/alice-a/"
    assert alice.degree == "2nd"
    assert alice.headline == "Software Engineer at LinkedIn"
    assert alice.location == "Sunnyvale, California"
    assert alice.summary.startswith("Software Engineer at LinkedIn since 2023")
    assert carol.degree == "3rd+"
    assert carol.headline == "SWE @ LinkedIn | CS @ Cornell'22"
    assert carol.summary is None
    assert dan.headline == "Senior Software Engineer @ LinkedIn"
    assert dan.location == "New York City Metropolitan Area"
    assert dan.summary.startswith("Senior Software Engineer at LinkedIn since March 2022")


def test_compact_has_no_spaces():
    assert _compact(["1337", "39939"]) == '["1337","39939"]'


def test_company_queries_and_pick():
    from referral.search import company_queries, pick_company

    assert company_queries("Zillow Group") == ["Zillow Group", "Zillow"]
    assert company_queries("Stripe") == ["Stripe"]
    cards = [
        {"slug": "mortech-inc", "name": "Mortech, a Zillow Group business", "text": "Mortech\n2,104 followers"},
        {"slug": "zillow", "name": "Zillow", "text": "Zillow\nReal Estate\n1.2M followers"},
        {"slug": "zillow-fan", "name": "Zillow Fans", "text": "Zillow Fans\n12K followers"},
    ]
    assert pick_company(cards, ["Zillow Group", "Zillow"])["slug"] == "zillow"  # exact name match
    no_exact = [cards[0], cards[2]]
    assert pick_company(no_exact, ["Zillow Group"])["slug"] == "zillow-fan"  # most followers


def test_company_match_never_picks_an_unrelated_company():
    from referral.jobs import clean_company
    from referral.search import company_queries, pick_company

    name = clean_company("Amazon.com Services LLC")
    assert name == "Amazon.com Services"
    assert company_queries(name) == ["Amazon.com Services", "Amazon"]
    assert company_queries("Amazon Web Services") == ["Amazon Web Services", "Amazon Web"]  # exact match comes first
    natura = {"slug": "natura", "name": "Natura &Co", "text": "Natura &Co\n2M followers"}
    amazon = {"slug": "amazon", "name": "Amazon", "text": "Amazon\n35M followers"}
    assert pick_company([natura], company_queries(name)) is None  # was picked before: most followers
    assert pick_company([natura, amazon], company_queries(name))["slug"] == "amazon"


def test_resolve_company_rejects_empty_name():
    import pytest

    from referral.search import resolve_company

    with pytest.raises(ValueError):
        asyncio.run(resolve_company(None, "  "))


def test_find_company_skips_stub_exact_match(monkeypatch):
    """Real results (2026-10-23): "Amazon.com Services" has an exact-name stub page with 183 followers."""
    import asyncio

    from referral import search

    results = {
        "Amazon.com Services": [{"slug": "amazon-devices-and-services", "name": "Amazon.com Services", "text": "183 followers"},
                                {"slug": "natura", "name": "Natura", "text": "3.4M followers"}],
        "Amazon": [{"slug": "amazon", "name": "Amazon", "text": "37M followers"},
                   {"slug": "amazon-web-services", "name": "Amazon Web Services (AWS)", "text": "11M followers"}],
    }
    current = {}

    async def goto(page, url):
        from urllib.parse import parse_qs, urlsplit
        current["q"] = parse_qs(urlsplit(url).query)["keywords"][0]

    class Page:
        async def evaluate(self, js):
            return results[current["q"]]

    async def no_sleep(*a):
        pass

    monkeypatch.setattr(search, "_goto", goto)
    monkeypatch.setattr(search.asyncio, "sleep", no_sleep)
    url = asyncio.run(search._find_company_url(Page(), "Amazon.com Services"))
    assert url == "https://www.linkedin.com/company/amazon/"

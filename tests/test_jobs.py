import re

from referral.jobs import _ROUTES, clean_url, html_to_text


def route(url):
    from urllib.parse import urlsplit

    parts = urlsplit(clean_url(url))
    for host, path, handler in _ROUTES:
        if (h := re.fullmatch(host, parts.netloc)) and (m := re.match(path, parts.path)):
            return handler.__name__, h.groups() + m.groups()
    return None


def test_clean_url_drops_tracking():
    assert (
        clean_url("https://jobs.smartrecruiters.com/LinkedIn3/744000156616169?utm_source=Simplify&ref=Simplify")
        == "https://jobs.smartrecruiters.com/LinkedIn3/744000156616169"
    )


def test_routes():
    assert route("https://jobs.smartrecruiters.com/LinkedIn3/744000156616169-software-engineer-intern") == (
        "_smartrecruiters", ("LinkedIn3", "744000156616169"))
    assert route("https://job-boards.greenhouse.io/anthropic/jobs/5456619008") == (
        "_greenhouse", ("anthropic", "5456619008"))
    assert route("https://boards.greenhouse.io/anthropic/jobs/5456619008?gh_src=x")[0] == "_greenhouse"
    assert route("https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd/apply")[0] == "_lever"
    assert route("https://jobs.ashbyhq.com/openai/8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3")[0] == "_ashby"
    assert route("https://zillow.wd5.myworkdayjobs.com/en-US/Zillow_Group_External/job/Seattle-WA-2nd-Ave/"
                 "Software-Development-Engineer---Intern_P751630?utm_source=Simplify&ref=Simplify") == (
        "_workday", ("zillow", "Zillow_Group_External", "Seattle-WA-2nd-Ave/Software-Development-Engineer---Intern_P751630"))
    assert route("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SWE-Intern_JR1")[0] == "_workday"
    assert route("https://careers.example.com/job/123") is None


def test_clean_company():
    from referral.jobs import clean_company

    assert clean_company("Zillow Group, Inc.") == "Zillow Group"
    assert clean_company("Acme LLC") == "Acme"
    assert clean_company("Coinbase") == "Coinbase"


def test_html_to_text():
    text = html_to_text("<p>Hi&nbsp;there</p><script>x()</script><ul><li>One</li><li>Two</li></ul>")
    assert "x()" not in text
    assert "- One" in text and "- Two" in text
    assert text.startswith("Hi there")

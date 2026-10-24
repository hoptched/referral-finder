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
    assert clean_company("00002 Citibank, N.A.") == "Citibank"
    assert clean_company("C01 Carrier") == "Carrier"
    assert clean_company("3M Company") == "3M Company" and clean_company("100 Thieves") == "100 Thieves"


def test_html_to_text():
    text = html_to_text("<p>Hi&nbsp;there</p><script>x()</script><ul><li>One</li><li>Two</li></ul>")
    assert "x()" not in text
    assert "- One" in text and "- Two" in text
    assert text.startswith("Hi there")


def test_json_ld_posting():
    from referral.jobs import _json_ld_posting

    page = """<html><head><script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "JobPosting", "title": "AI Engineer Intern",
     "hiringOrganization": {"@type": "Organization", "name": "Acme Corp"},
     "jobLocation": {"@type": "Place", "address": {"addressLocality": "Chicago", "addressRegion": "IL"}},
     "description": "&lt;p&gt;Build things.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"}
    </script></head><body>Loading…</body></html>"""
    p = _json_ld_posting("https://jobs.acme.com/1", page)
    assert (p.source, p.title, p.company, p.location) == ("json-ld", "AI Engineer Intern", "Acme", "Chicago, IL")
    assert "Build things." in p.description and "- Python" in p.description
    assert _json_ld_posting("https://x", "<html><body>Log into your account</body></html>") is None


def test_clean_url_keeps_job_ids():
    ibm = ("https://careers.ibm.com/en_US/careers/JobDetail?jobId=137753&utm_source=zero2sudo"
           "&fbclid=PAZXh0bgNhZW0CMTEAcGRvZgRz#apply")
    assert clean_url(ibm) == "https://careers.ibm.com/en_US/careers/JobDetail?jobId=137753"
    assert clean_url("https://boards.greenhouse.io/acme?gh_jid=123&gh_src=abc") == "https://boards.greenhouse.io/acme?gh_jid=123"
    assert clean_url("https://x.com/job/1?utm_medium=a&ref=Simplify&src=Simplify") == "https://x.com/job/1"

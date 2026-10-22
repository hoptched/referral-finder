from datetime import date

from referral.profiles import Education, Experience, Profile
from referral.rank import apply_profile, rank, recency_points, score_card
from referral.search import SearchResult

ROLES = ["software engineer"]
SCHOOL = "Cornell University"


def result(headline, summary=None, degree="3rd+", slug="x"):
    return SearchResult(name=slug, linkedin_url=f"https://www.linkedin.com/in/{slug}/", degree=degree,
                        headline=headline, location=None, summary=summary)


def test_junior_engineer_with_recent_year_beats_principal():
    junior = score_card(result("SWE @ LinkedIn | CS @ Cornell’22",
                               "Software Engineer at LinkedIn since 2022."), ROLES, None, SCHOOL)
    principal = score_card(result("Principal Staff Software Engineer at LinkedIn",
                                  "Principal Staff Software Engineer at LinkedIn since 2015."), ROLES, None, SCHOOL)
    assert junior.grad_year == 2022
    assert junior.seniority == "junior"
    assert principal.seniority == "principal"
    assert rank([principal, junior])[0] is junior


def test_summary_title_beats_vague_headline():
    # Headline says nothing about level; the summary says Senior Staff.
    c = score_card(result("Engineer@LinkedIn", "Senior Staff Software Engineer at LinkedIn since March 2025."), ROLES, None, SCHOOL)
    assert c.seniority == "staff"


def test_recruiter_and_connection():
    c = score_card(result("Technical Recruiter at LinkedIn", degree="2nd"), ROLES, None, SCHOOL)
    assert c.seniority == "recruiter"
    assert "+1 2nd connection" in c.reasons


def test_recency_points():
    today = date(2026, 10, 21)
    assert recency_points(2024, today)[0] == 3
    assert recency_points(2020, today)[0] == 2
    assert recency_points(2010, today)[0] == 0
    assert recency_points(2028, today)[0] == 1


def test_apply_profile_fills_grad_year_and_flags_missing_school():
    c = score_card(result("Software Engineer at LinkedIn", "Software Engineer at LinkedIn since 2023."), ROLES, None, SCHOOL)
    base = c.score
    profile = Profile(
        linkedin_url="https://www.linkedin.com/in/x/", name="X",
        educations=[Education(school="Cornell University", degree="BS", field_of_study="CS", start_year=2019, end_year=2023)],
        experiences=[Experience(title="Software Engineer", company="LinkedIn", employment_type=None, start="2023",
                                end=None, is_current=True, location=None, description=None)],
    )
    apply_profile(c, profile, "Cornell University", "LinkedIn")
    assert c.verified and c.grad_year == 2023 and c.score > base

    other = score_card(result("Software Engineer at LinkedIn"), ROLES, None, SCHOOL)
    apply_profile(other, profile.model_copy(update={"educations": []}), "Cornell University", "LinkedIn")
    assert other.verified is False and other.score < 0


def test_company_matches_short_and_long_names():
    from referral.rank import company_matches

    assert company_matches("Zillow Group", "Zillow")
    assert company_matches("Zillow", "Zillow Group")
    assert company_matches("LinkedIn", "LinkedIn")
    assert not company_matches("Zillow Group", "Redfin")
    assert not company_matches("Zillow Group", "")


def test_card_year_for_other_schools():
    from referral.config import school_short
    from referral.rank import card_year_re

    assert school_short("University of Michigan") == "Michigan"
    assert school_short("Cornell University") == "Cornell"
    assert card_year_re("University of Michigan").search("SWE @ Google | Michigan '23").group(1) == "23"
    assert card_year_re("Cornell University").search("SWE | Michigan '23") is None

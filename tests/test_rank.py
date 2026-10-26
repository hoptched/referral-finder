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


def _edu(degree, school="Cornell University", end=2022):
    return Education(school=school, degree=degree, field_of_study=None, start_year=None, end_year=end)


def test_degree_levels():
    from referral.rank import degree_level

    for d in ["Bachelor of Science - BS", "Bachelor's degree", "BA", "B.S.", "Bachelor of Engineering - BE"]:
        assert degree_level(_edu(d)) == "undergrad", d
    for d in ["Master of Engineering - MEng", "Master's degree", "M.Eng", "M.S.", "Master of Business Administration - MBA",
              "Master of Professional Studies"]:
        assert degree_level(_edu(d)) == "masters", d
    assert degree_level(_edu("Doctor of Philosophy - PhD")) == "phd"
    for d in ["Certificate", "Data Analytics Certificate", "Change Management Certification", "Summer Program",
              "Corporate Communication Certificate, eCornell"]:
        assert degree_level(_edu(d)) == "certificate", d
    assert degree_level(_edu(None)) == "unknown"


def _profile_with(*educations):
    return Profile(linkedin_url="https://www.linkedin.com/in/x/", name="X", educations=list(educations),
                   experiences=[Experience(title="Software Engineer", company="LinkedIn", employment_type=None,
                                           start="2023", end=None, is_current=True, location=None, description=None)])


def test_undergrad_beats_masters_and_certificate_is_penalized():
    def scored(*educations):
        c = score_card(result("Software Engineer at LinkedIn", "Software Engineer at LinkedIn since 2023."), ROLES, None, SCHOOL)
        return apply_profile(c, _profile_with(*educations), SCHOOL, "LinkedIn")

    undergrad = scored(_edu("Bachelor of Science - BS"))
    masters = scored(_edu("Master of Engineering - MEng"))
    cert = scored(_edu("Data Analytics Certificate"))
    both = scored(_edu("Data Analytics Certificate", end=2025), _edu("Bachelor of Arts - BA", end=2019))
    assert undergrad.degree == "undergrad" and masters.degree == "masters" and cert.degree == "certificate"
    assert undergrad.score > masters.score > cert.score
    assert masters.score - cert.score == 9  # master's +1 vs certificate -8
    assert both.degree == "undergrad" and both.grad_year == 2019  # best level wins; year from the real degree

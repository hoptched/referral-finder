"""Draft a referral-request email from template.md.

The template's subject and body are filled mechanically; Claude (models.compose)
writes only {personal_line} and {blurb}, guided by the template's
# Instructions section, the job, the recipient's profile and the resume PDF.
That keeps the job link, the ask and the sign-off exactly as the user wrote them.
"""

import base64
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from referral.config import project_path, school_short
from referral.llm import client, log_usage

TEMPLATE_PATH = project_path("template.md")
EXAMPLE_TEMPLATE_PATH = project_path("template.example.md")
JOB_DESCRIPTION_LIMIT = 8000  # characters; postings are mostly boilerplate past this


class Template(BaseModel):
    subject: str
    body: str
    instructions: str


class _Written(BaseModel):
    personal_line: str
    blurb: str
    company_name: str  # display name, e.g. "OpenAI" when the posting only gave "Openai"
    short_title: str  # role without location/season/req-number noise, for the subject line


class Draft(BaseModel):
    to: str | None
    subject: str
    body: str
    short_title: str
    company: str
    generated_at: datetime
    edited: bool = False


def load_template(path: Path = TEMPLATE_PATH) -> Template:
    if not path.exists():  # a fresh checkout: use the example until the user makes their own
        path = EXAMPLE_TEMPLATE_PATH
    text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S)
    sections = dict(re.findall(r"^# (\w+)\s*\n(.*?)(?=^# \w+\s*$|\Z)", text, re.M | re.S))
    missing = {"Subject", "Body", "Instructions"} - sections.keys()
    if missing:
        raise ValueError(f"template.md is missing section(s): {', '.join(sorted(missing))}")
    return Template(subject=sections["Subject"].strip(), body=sections["Body"].strip(),
                    instructions=sections["Instructions"].strip())


def first_name(name: str) -> str:
    return re.split(r"[\s(]", name.strip(), maxsplit=1)[0]


def _fill(text: str, values: dict) -> str:
    # Only known {placeholders}; any other braces in the template are left alone.
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def _recipient_summary(candidate: dict, school: str) -> str:
    r = candidate["result"]
    lines = [f"Name: {r['name']}", f"Headline: {r.get('headline') or ''}"]
    if r.get("summary"):
        # LinkedIn appends "You both went to/worked at X." from the viewer's point of view. It's
        # often wrong for me (e.g. "You both worked at <school>"), and Claude repeats it, so drop it.
        summary = re.sub(r"\s*You both [^.]*\.", "", r["summary"]).strip()
        lines.append(f"LinkedIn summary: {summary}")
    if candidate.get("grad_year"):
        lines.append(f"{school} class of {candidate['grad_year']}")
    profile = candidate.get("profile") or {}
    for e in profile.get("educations", []):
        lines.append(f"Education: {e['school']}, {e.get('degree') or ''} {e.get('field_of_study') or ''}".strip())
    for x in profile.get("experiences", [])[:4]:
        when = f"{x.get('start') or '?'} – {x.get('end') or 'Present'}"
        desc = f" ({x['description']})" if x.get("description") else ""
        lines.append(f"Role: {x['title']} at {x['company']}, {when}{desc}")
    return "\n".join(lines)


async def compose(cfg: dict, job: dict, candidate: dict, to: str | None) -> Draft:
    template = load_template()
    me = cfg["me"]
    name = candidate["result"]["name"]
    values = {
        "first_name": first_name(name),
        "my_name": me["name"],
        "my_first_name": first_name(me["name"]),
        "application_email": me.get("application_email", ""),
        "school_short": school_short(me["school"]),
        "major": me["major"],
        "class_year": me["class_year"],
        "class_year_short": f"{int(me['class_year']) % 100:02d}",
        "school": me["school"],
        "job_title": job["title"],
        "company": job["company"],
        "job_link": job["url"],
    }
    resume = base64.standard_b64encode(project_path(me["resume_path"]).read_bytes()).decode()
    prompt = f"""I'm asking a {me['school']} alum for a referral. Write two pieces of my email, following the instructions.

<instructions>
{_fill(template.instructions, values)}
</instructions>

<email_template>
{_fill(template.body, values)}
</email_template>

<me>
{me['name']}, {me['school']} {me['major']}, class of {me['class_year']}. My resume is attached.
</me>

<recipient>
{_recipient_summary(candidate, me["school"])}
</recipient>

<job>
{job['title']} at {job['company']}{f" ({job['location']})" if job.get('location') else ''}
{job.get('description', '')[:JOB_DESCRIPTION_LIMIT]}
</job>

Also return:
- company_name: the company's usual display name (fix capitalization like "Openai" → "OpenAI").
- short_title: the role as a person would say it, without location, season, or requisition
  numbers (e.g. "Software Engineer Intern")."""

    response = await client().beta.messages.parse(
        model=cfg["models"]["compose"],
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": resume},
                 "title": "My resume"},
                {"type": "text", "text": prompt},
            ],
        }],
        output_format=_Written,
    )
    log_usage("draft", response)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this email. Try again or write it by hand.")
    written = response.parsed_output
    values.update(company=written.company_name, short_title=written.short_title.strip(),
                  personal_line=written.personal_line.strip(), blurb=written.blurb.strip())
    return Draft(
        to=to,
        subject=_fill(template.subject, values),
        body=_fill(template.body, values),
        short_title=values["short_title"],
        company=values["company"],
        generated_at=datetime.now(),
    )

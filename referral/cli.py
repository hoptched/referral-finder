"""Command-line entry point: `refer <command>`."""

import asyncio
from pathlib import Path
from typing import Optional

import typer
from linkedin_scraper import JobScraper
from rich.console import Console
from rich.table import Table

from referral.config import load_config, project_path
from referral.jobs import parse_job
from referral.linkedin import check_session, create_session, open_session
from referral.profiles import scrape_profile
from referral.pipeline import find_candidates, write_run
from referral.search import resolve_company, search_alumni

app = typer.Typer(help="Find alumni of your school at a company and request referrals.")
console = Console()


def _session_path():
    return project_path(load_config()["linkedin"]["session_path"])


@app.command()
def login():
    """Open a browser to log in to LinkedIn and save the session."""
    path = _session_path()
    console.print("A browser window will open. Log in (including 2FA) and wait for your feed to load.")
    console.print("You have 5 minutes.")
    asyncio.run(create_session(path))
    console.print(f"[green]Session saved to {path}[/green]")


@app.command()
def check(
    profile: Optional[str] = typer.Option(None, help="LinkedIn profile URL to test-scrape"),
    job: Optional[str] = typer.Option(None, help="LinkedIn job URL to test-scrape"),
):
    """Verify the saved LinkedIn session, optionally test-scraping a profile or job."""
    path = _session_path()
    headless = load_config()["linkedin"]["headless"]
    if not path.exists():
        console.print("[red]No LinkedIn session found. Run `refer login` first.[/red]")
        raise typer.Exit(1)
    if not asyncio.run(check_session(path, headless)):
        console.print("[red]Session is not logged in. Run `refer login`.[/red]")
        raise typer.Exit(1)
    console.print("[green]Session is logged in.[/green]")

    if profile or job:
        asyncio.run(_test_scrape(path, headless, profile, job))


async def _test_scrape(path, headless, profile, job):
    model = load_config()["models"]["extract"]
    async with open_session(path, headless) as browser:
        if profile:
            person = await scrape_profile(browser.page, profile, model)
            console.print(f"[bold]{person.name}[/bold] ({person.linkedin_url})")
            for e in person.educations:
                console.print(f"  edu: {e.school} — {e.degree or ''} {e.field_of_study or ''} ({e.start_year}–{e.end_year})")
            for x in person.experiences[:3]:
                now = " [current]" if x.is_current else ""
                console.print(f"  exp: {x.title} @ {x.company} ({x.start} – {x.end}){now}")
        if job:
            j = await JobScraper(browser.page).scrape(job)
            console.print(f"[bold]{j.job_title}[/bold] @ {j.company} ({j.location})")


@app.command()
def job(
    url: str,
    full: bool = typer.Option(False, "--full", help="Print the whole description"),
):
    """Parse a job posting URL and print what was extracted."""
    cfg = load_config()
    posting = asyncio.run(parse_job(url, cfg["models"]["extract"], _session_path()))
    console.print(f"[bold]{posting.title}[/bold] @ {posting.company}  [dim]({posting.source})[/dim]")
    for warning in posting.warnings:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for label, value in [("Department", posting.department), ("Team", posting.team), ("Location", posting.location)]:
        if value:
            console.print(f"  {label}: {value}")
    console.print(f"  URL: {posting.url}")
    desc = posting.description
    console.print(f"\n{desc if full or len(desc) <= 600 else desc[:600] + ' …'}")


@app.command()
def search(
    url: str,
    keywords: Optional[str] = typer.Option(None, help="Search keywords (default: first of target_roles)"),
    pages: Optional[int] = typer.Option(None, help="Result pages to read (default: linkedin.search_pages)"),
    company_url: Optional[str] = typer.Option(None, help="LinkedIn company URL, if name lookup picks the wrong one"),
):
    """Find alumni of your school currently at the company behind a job posting."""
    cfg = load_config()
    posting = asyncio.run(parse_job(url, cfg["models"]["extract"], _session_path()))
    console.print(f"[bold]{posting.title}[/bold] @ {posting.company}")
    asyncio.run(_search(cfg, posting.company, company_url, keywords or cfg["target_roles"][0],
                        pages or cfg["linkedin"]["search_pages"]))


async def _search(cfg, company_name, company_url, keywords, pages):
    async with open_session(_session_path(), cfg["linkedin"]["headless"]) as browser:
        company = await resolve_company(browser.page, company_name, company_url, cfg["models"]["extract"])
        console.print(f"LinkedIn company: {company.linkedin_url} (ids {', '.join(company.ids)})")
        results = await search_alumni(browser.page, company, cfg["me"]["linkedin_school_id"], keywords, pages)
    console.print(f"\n{len(results)} alumni matching {keywords!r}:\n")
    for r in results:
        console.print(f"[bold]{r.name}[/bold] [dim]{r.degree or ''}[/dim] — {r.headline}")
        console.print(f"  {r.location or ''}  {r.linkedin_url}")
        if r.summary:
            console.print(f"  [dim]{r.summary}[/dim]")


@app.command()
def find(
    url: str,
    keywords: Optional[str] = typer.Option(None, help="Search keywords (default: first of target_roles)"),
    pages: Optional[int] = typer.Option(None, help="Result pages to read (default: linkedin.search_pages)"),
    verify: Optional[int] = typer.Option(None, help="Top N profiles to scrape (default: linkedin.profiles_to_verify; 0 = none)"),
    company_url: Optional[str] = typer.Option(None, help="LinkedIn company URL, if name lookup picks the wrong one"),
):
    """Parse a job, find and rank alumni of your school there, and pick who to contact."""
    cfg = load_config()
    posting = asyncio.run(parse_job(url, cfg["models"]["extract"], _session_path()))
    console.print(f"[bold]{posting.title}[/bold] @ {posting.company}")
    candidates = asyncio.run(find_candidates(cfg, posting, company_url, keywords, pages, verify, log=console.print))
    if not candidates:
        console.print("[yellow]No alumni found.[/yellow]")
        raise typer.Exit()

    _print_candidates(candidates)
    picks = _ask_picks(len(candidates))
    path = write_run(posting, candidates, [candidates[i].result.linkedin_url for i in picks])
    console.print(f"\nSelected {len(picks)}. Saved to {path}")


def _print_candidates(candidates):
    table = Table(show_lines=False)
    for col in ("#", "Name", "Title", "Grad", "Location", "Check", "Score", "Why"):
        table.add_column(col, overflow="fold")
    for i, c in enumerate(candidates, 1):
        check = {True: "[green]✓[/green]", False: "[red]✗[/red]", None: "[dim]–[/dim]"}[c.verified]
        table.add_row(
            str(i), c.result.name, c.result.headline or "", str(c.grad_year or ""), c.result.location or "",
            check, str(c.score), ", ".join(r.split(" ", 1)[1] for r in c.reasons),
        )
    console.print(table)
    console.print("[dim]Check: ✓ school confirmed on profile, ✗ not found, – not scraped[/dim]")


def _ask_picks(n: int) -> list[int]:
    while True:
        raw = typer.prompt("Who to contact? (e.g. 1,3,5 or 1-4; blank for none)", default="", show_default=False)
        try:
            picks: list[int] = []
            for part in filter(None, (p.strip() for p in raw.split(","))):
                lo, _, hi = part.partition("-")
                picks.extend(range(int(lo), int(hi or lo) + 1))
            if all(1 <= p <= n for p in picks):
                return [p - 1 for p in dict.fromkeys(picks)]
        except ValueError:
            pass
        console.print(f"[red]Enter numbers between 1 and {n}.[/red]")


@app.command("gmail-login")
def gmail_login(
    finish: Optional[str] = typer.Option(None, help="The full localhost:8090 address your browser ended up on"),
):
    """Connect your Gmail account for sending (two steps: get link, then --finish with the redirect URL)."""
    from referral import gmail

    if finish is None:
        console.print("1. Open this link in your browser and approve:\n")
        print(gmail.start_login())  # plain print: no wrapping, so the URL copies cleanly
        console.print("\n2. The browser will end on a 'site can't be reached' page at localhost:8090. That's expected.")
        console.print('   Copy that whole address and run:  refer gmail-login --finish "<address>"')
        return
    account = gmail.finish_login(finish)
    console.print(f"[green]Gmail connected as {account}. Token saved to {gmail.TOKEN_PATH}[/green]")


@app.command("ig-login")
def ig_login():
    """Open a browser to log in to Instagram (use a spare account) and save the session."""
    from referral import instagram

    path = instagram.session_path(load_config())
    console.print("A browser window will open. Log in to Instagram with a spare account (not your main one).")
    console.print("You have 5 minutes. The window closes by itself once you're logged in.")
    asyncio.run(instagram.create_session(path))
    console.print(f"[green]Instagram session saved to {path}[/green]")


@app.command("ig-fetch")
def ig_fetch(force: bool = typer.Option(False, "--force", help="Check even if the last check was under a day ago")):
    """Read today's Instagram stories and add the jobs they link to (runs at most once a day)."""
    from referral import instagram

    cfg = load_config()
    if not force and not instagram.due(cfg):
        state = instagram.load_state()
        console.print(f"Not due yet (last check {state.get('last_run')}). Use --force to check anyway.")
        return
    state = asyncio.run(instagram.check(cfg, log=console.print))
    if state.get("error"):
        raise typer.Exit(1)


@app.command("setup-check")
def setup_check():
    """Check that everything needed to run is in place, and say how to fix what isn't."""
    import os

    from referral import gmail
    from referral.config import ROOT

    rows: list[tuple[str, bool, str]] = []  # (item, ok, how to fix / detail)
    env = ROOT / ".env"
    rows.append((".env file", env.exists(), "cp .env.example .env, then fill in the keys"))
    rows.append(("ANTHROPIC_API_KEY", bool(os.environ.get("ANTHROPIC_API_KEY")), "required: console.anthropic.com → API keys"))
    hunter, apollo = bool(os.environ.get("HUNTER_API_KEY")), bool(os.environ.get("APOLLO_API_KEY"))
    rows.append(("HUNTER_API_KEY", hunter, "optional: hunter.io → API (email lookup)"))
    rows.append(("APOLLO_API_KEY", apollo, "optional: apollo.io → Settings → Integrations → API"))
    cfg = None
    try:
        cfg = load_config()
        rows.append(("config.yaml", True, ""))
    except FileNotFoundError:
        rows.append(("config.yaml", False, "cp config.example.yaml config.yaml, then edit the me: section"))
    if cfg:
        me = cfg.get("me", {})
        placeholder = me.get("name") == "Alex Doe"
        rows.append(("config.yaml me: filled in", not placeholder, "replace the example name/email with yours"))
        resume = project_path(me.get("resume_path", "resume.pdf"))
        rows.append((f"resume ({resume.name})", resume.exists(), f"put your resume PDF at {resume}"))
        session = project_path(cfg["linkedin"]["session_path"])
        rows.append(("LinkedIn login (session.json)", session.exists(), "run: refer login"))
        from referral import instagram
        rows.append(("Instagram login (stories → jobs)", instagram.session_path(cfg).exists(),
                     "optional: refer ig-login (with a spare account)"))
    rows.append(("template.md", project_path("template.md").exists(),
                 "optional: cp template.example.md template.md (the example is used until then)"))
    browsers = Path.home() / ".cache" / "ms-playwright"
    rows.append(("Playwright Chromium", any(browsers.glob("chromium*")), "run: .venv/bin/playwright install chromium"))
    rows.append(("Gmail credentials.json", gmail.CREDENTIALS_PATH.exists(), "optional: see README → Gmail setup"))
    status = gmail.status()
    rows.append(("Gmail login", status["connected"], status["error"] or "optional: refer gmail-login"))

    required = {".env file", "ANTHROPIC_API_KEY", "config.yaml", "config.yaml me: filled in", "LinkedIn login (session.json)",
                "Playwright Chromium"}
    table = Table(show_header=True)
    for col in ("", "Item", "How to fix"):
        table.add_column(col)
    for item, ok, fix in rows:
        mark = "[green]✓[/green]" if ok else ("[red]✗[/red]" if item in required or item.startswith("resume") else "[yellow]–[/yellow]")
        table.add_row(mark, item, "" if ok else fix)
    console.print(table)
    if not (hunter or apollo):
        console.print("[yellow]No email source set: you'll have to add emails by hand.[/yellow]")
    missing = [i for i, ok, _ in rows if not ok and (i in required or i.startswith("resume"))]
    if missing:
        console.print(f"[red]Not ready: {', '.join(missing)}[/red]")
        raise typer.Exit(1)
    console.print("[green]Ready. Run: refer web[/green]")


@app.command()
def web(port: int = typer.Option(8000, help="Port to serve on")):
    """Start the web UI at http://localhost:<port> (local only)."""
    import uvicorn

    console.print(f"Open http://localhost:{port} in your browser. Ctrl+C to stop.")
    # 127.0.0.1 only: the app drives your LinkedIn session and reads your keys.
    uvicorn.run("referral.web:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()

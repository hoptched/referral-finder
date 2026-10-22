"""Command-line entry point: `refer <command>`."""

import asyncio
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
        company = await resolve_company(browser.page, company_name, company_url)
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


@app.command()
def web(port: int = typer.Option(8000, help="Port to serve on")):
    """Start the web UI at http://localhost:<port> (local only)."""
    import uvicorn

    console.print(f"Open http://localhost:{port} in your browser. Ctrl+C to stop.")
    # 127.0.0.1 only: the app drives your LinkedIn session and reads your keys.
    uvicorn.run("referral.web:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()

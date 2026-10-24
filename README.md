# referral-finder

A small local website for asking alumni for job referrals. It lists open internships from [SimplifyJobs' list](https://github.com/SimplifyJobs/Summer2027-Internships); after you apply to one, drag it into the search queue (or paste any job link). It:

1. reads the posting (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, LinkedIn, or any job page),
2. finds alumni of **your school** who currently work at that company, via LinkedIn search, and ranks them,
3. lets you click who to contact,
4. looks up their work emails (Hunter.io, then Apollo.io),
5. drafts a personal referral email for everyone with an email (Claude),
6. sends the emails from your Gmail with your resume attached, only after you approve them.

Everything runs on your own computer at `http://localhost:8000`. See [AGENTS.md](AGENTS.md) for the build log and design decisions.

## Using the website

```bash
.venv/bin/refer web          # then open http://localhost:8000 (works from Windows when running in WSL)
```

The page has three columns: **Jobs to apply to**, **Past searches**, and the open search.

1. **Jobs to apply to** (far left) lists open internships from SimplifyJobs' GitHub list (`.github/scripts/listings.json`), newest first, filtered by the `simplify` settings in `config.yaml`: by default Summer 2027, Software and AI/ML/Data, **posted in the last 10 days** (`max_age_days`), **US locations only** (a job with at least one US location stays), **no PhD-only jobs** (PhD is the only degree Simplify lists, or "PhD" is in the title), and **internships only** (`internships_only`: titles like "New Grad", "Full-Time" or "Entry Level" without "Intern"/"Co-op" are hidden). Each has its age, location, an **Apply ↗** link and a **+** button. The filter box searches company, role and location. The list is downloaded at most once an hour (cached in `.cache/simplify.json`); ↻ reloads it now. Jobs that already have a search, or are in the queue, are hidden (matched by Simplify ID, or by link ignoring tracking parameters).
2. **Drag a job into Past searches** (or click **+**) to queue a search for alumni. You can also paste any job link at the top and click **Find alumni** (if a search picks the wrong LinkedIn company, rerun it from the command line with `refer find <url> --company-url <linkedin company URL>`), or drop a link dragged from another browser tab. Searches run one at a time, top of the queue first (about 1–3 minutes each; slower the first time for a company, and when new profiles need checking). Queued and running searches show above Past searches with their latest progress line; click one to see its full progress. A failed search stays there with **Retry** / **Remove**. The queue is saved in `.data/queue.json`, so restarting `refer web` picks it up again. A pasted link opens its results when its search finishes; dragged jobs just show up in Past searches.
3. The people show up as cards, ranked. Each card has their title, class year, location, whether your school was confirmed on their profile, why they ranked where they did, and a link to their LinkedIn profile.
4. **Click a card to select it.** Click again to unselect. Selections save immediately.
5. Opening a past search costs no LinkedIn traffic. Searches whose outreach is done (everyone selected has been looked up, and everyone with an email has a draft) are **green**. Each entry shows the company (hover for the job title), the date you searched, and, in green, how many emails you've sent for it. The job posting link is at the top of an open search (**Open job posting ↗**). Clicking **Outreach** on a finished search asks for confirmation first. Each search also has an **Applied** checkbox for keeping track of where you've applied. It saves right away, and searching the same job again keeps it.
6. Open the **Outreach** tab. It's disabled until you've selected someone. Opening it automatically:
   - looks up emails for selected people (Hunter, then Apollo; each method at most once per person, so reopening costs nothing),
   - **drafts emails only for people whose email was found** (Sonnet).

   Layout: **Send all emails (N)** at the top, then a card per person with an email (address and source badge, editable subject/body, Send / Test to me / Copy / Regenerate), then **No email found**: each person there has a profile link and a "Found their email? Add it" box that drafts their email.
   - **Emails are scheduled, not sent right away.** **Send today/tomorrow 11:00 AM** on a card, or **Schedule all emails (N)** at the top, queues them for the next `email.send_time` (default 11:00 AM, `email.timezone` America/New_York). The site sends them at that time, 20–60 s apart, with your resume attached. Until then the card shows "Scheduled for …" with **Cancel** and **Send now**, and you can still edit the draft: the version saved at send time is what goes out. The sidebar shows "N scheduled". The site must be running then (see "Taskbar shortcut"): if it was off, the emails go out when it starts, up to 3 hours late, and after that they move to the next day's send time. If a send fails (e.g. Gmail login expired), the card says why and it isn't retried; use **Send now** after fixing it. Each person can only be emailed once, across all searches, and can only be scheduled once.
   - **Send now** sends one email immediately (after a confirmation); **Test to me** sends a `[TEST]` copy to your own Gmail.

The top-right panel shows what's left and when it resets: **Email credits** (Hunter + Apollo as one pool; hover for the breakdown; Hunter's reset date comes from its API, Apollo's from `apollo.reset_day`), LinkedIn profile checks left today (resets at midnight), Claude spend this month (resets on the 1st), and the Gmail account. Items running low turn red. It refreshes after searches, outreach loads and sends (`GET /api/credits`).

The server only listens on `127.0.0.1`, because it uses your LinkedIn session and API keys. Only one search runs at a time.

## Jobs from Instagram stories (optional)

Some creators (by default **@zero2sudo**, set in `instagram.accounts`) post job application links in their Instagram stories every day. The site can read those stories once a day and add the jobs to **Jobs to apply to**, tagged **@zero2sudo**.

1. **Use a spare Instagram account**, not your main one. Stories can't be seen logged out, scraping is against Instagram's terms, and Instagram can lock an account for it. The spare account should follow the creators you list.
2. Run `.venv/bin/refer ig-login`. A browser opens; log in (2FA included, 5 minutes). The window closes by itself and the session is saved to `ig_session.json` (gitignored).
3. That's it. While `refer web` is open, it checks every live story once a day (`instagram.check_every_hours`, default 24), right after startup if one is due, then every 24 hours (it looks every 30 minutes). `.venv/bin/refer ig-fetch` runs the same check by hand (add `--force` to ignore the once-a-day limit).

How a check works: it opens the account's story page in the logged-in (hidden) browser, like tapping their story, and reads the story data Instagram's own page loads, then each story's image. It makes no direct API calls; those got a new account rate limited. The spare account should follow only the creators you list: the story viewer preloads other accounts' stories, and those are skipped but still loaded. Each **new** story goes to Claude (`models.extract`, Haiku, about $0.002 a story) with its image and its link stickers, after following redirects like bit.ly. Claude says which links are job openings and reads the company, role and location. Only links that are actually on the story are kept, and a job with no link is dropped. Story jobs go through the same filters as the Simplify list (age, US only, no PhD-only). With `internships_only`, Claude also says whether each one is an internship and which term it's for: new-grad/full-time roles are hidden, and so are internships for a term not in `simplify.terms` (one with no term shown is kept). A job that's also on Simplify shows once, with both tags. The status line under the list shows the last check, or the problem (e.g. "session expired. Run: refer ig-login"). After a failure it waits 6 hours before trying again, or a full day after a rate limit (`refer ig-fetch --force` overrides this; don't use it right after a rate limit).

Stories disappear after 24 hours, so **a day with the site closed is a day of stories missed**. To check even when the site isn't open, schedule `refer ig-fetch` yourself (e.g. Windows Task Scheduler running `wsl.exe -e /path/to/referral-finder/.venv/bin/refer ig-fetch` daily); it does nothing if a check already ran in the last 24 hours.

Files: `.data/instagram.json` (jobs found, stories already read, last check), `.cache/instagram/<account>.json` (the last raw story data, for debugging).

## Getting started on a new machine

About 30–45 minutes, most of it creating accounts. **Forking?** Everything personal stays out of git: your keys go in `.env`, your details in `config.yaml`, your email in `template.md`, and your resume, logins and search results in gitignored files. The repo only has `*.example` versions, so a fork starts clean and each person adds their own keys.

### 1. What you need

- **Linux, macOS, or Windows with WSL2.** On WSL, the one-time LinkedIn login opens a browser window, which needs WSLg (built into Windows 11).
- **Python 3.10+** and **git**.
- **[uv](https://docs.astral.sh/uv/)** (Python package manager): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Accounts:**

| Account | Used for | Cost | Required? |
|---|---|---|---|
| LinkedIn | finding alumni (your normal account) | free | yes |
| [Anthropic API](https://console.anthropic.com) | reading profiles (Haiku), writing emails (Sonnet) | pay-as-you-go, about $0.06 per job | yes |
| [Hunter.io](https://hunter.io) | finding work emails | free plan: 50 credits/month | recommended (without any email source, you can only add emails by hand) |
| [Apollo.io](https://apollo.io) | second email source when Hunter misses | free plan: 75 credits/month | optional |
| [Google Cloud](https://console.cloud.google.com) | sending through your Gmail | free | optional (without it, copy and paste emails) |

LinkedIn's terms don't allow scraping. The tool keeps volume low (a daily cap, random delays, caching), but the account you log in with is the one at risk.

### 2. Install

```bash
git clone <this repo> referral-finder
cd referral-finder
uv venv .venv
uv pip install -p .venv -e ".[dev]"
.venv/bin/playwright install chromium
# If Chromium complains about missing system libraries (fresh Linux/WSL):
#   sudo .venv/bin/playwright install-deps chromium
.venv/bin/pytest tests        # all tests should pass; they need no keys or network
```

### 3. API keys → `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Fill in:
- `ANTHROPIC_API_KEY`: Console → API keys. If the console says your key isn't scoped to a workspace (requests fail with "must include the anthropic-workspace-id header"), also set `ANTHROPIC_WORKSPACE_ID` (Console → Settings → Workspaces, starts with `wrkspc_`).
- `HUNTER_API_KEY` (optional): hunter.io → API. Leave empty to turn Hunter off.
- `APOLLO_API_KEY` (optional): apollo.io → Settings → Integrations → API. Give the key access to People Enrichment, or make it a master key. Apollo only gives free accounts API access if you **signed up with a work or school email**. Leave the line empty to turn Apollo off.

`.env` is gitignored. Never commit it.

### 4. Your profile → `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Edit the `me:` section: name, the email you apply to jobs with, school (as written on LinkedIn), class year, major, and `linkedin_school_id`. To find your school's ID, open your school's LinkedIn page and click **Alumni**; the URL contains `schoolFilter=["<ID>"]` (Cornell University is `3523`). Set `target_roles` to the roles you're applying for. If you use Apollo, set `apollo.monthly_credit_limit` and `apollo.reset_day` from apollo.io → Settings → Billing and credits.

### 5. Your resume and email template

```bash
cp ~/path/to/your_resume.pdf resume.pdf     # gitignored; attached to emails and read by Claude
cp template.example.md template.md          # gitignored; your email, edit freely
```

`template.md` has three sections: `# Subject` and `# Body` are filled in exactly as written; `# Instructions` guides the two parts Claude writes (a personal line and a short blurb about you). Until you create `template.md`, the example is used.

### 6. Log in to LinkedIn (once)

```bash
.venv/bin/refer login      # a browser opens: log in (2FA included), wait for your feed
.venv/bin/refer check      # should print "Session is logged in."
```

This saves `session.json` (gitignored). If LinkedIn logs you out later, run `refer login` again.

### 7. Connect Gmail (optional)

Follow [Gmail setup](#gmail-setup-one-time) below. Without it, the site still drafts everything, and you copy and paste.

### 8. Check your setup, then run it

```bash
.venv/bin/refer setup-check
```

This lists every requirement with ✓ / ✗ and how to fix each one (keys, config, resume, LinkedIn login, browser, Gmail). Fix anything marked ✗; items marked – are optional.

### 9. Run it

```bash
.venv/bin/refer web
```

Open http://localhost:8000. On WSL, open it in your normal Windows browser. **After pulling code changes, stop it (Ctrl+C) and start it again**; it doesn't reload on its own.

### Taskbar shortcut (Windows + WSL)

A **Referral Finder** shortcut starts the site in WSL (if it isn't running yet) and opens it in your browser. Create it once from **Windows PowerShell** (not the WSL terminal):

```powershell
powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\<you>\<path to>\referral-finder\scripts\windows\install-shortcut.ps1 -Distro Ubuntu -Folder "~/<path to>/referral-finder"
```

- This puts the shortcut on your Desktop and in the Start menu, plus a copy of `scripts/windows/start-referral-finder.ps1` in `%LOCALAPPDATA%\ReferralFinder` (Windows won't run scripts straight from a `\\wsl.localhost` path). Run it again after changing either script.
- **Pin it:** right-click the Desktop shortcut → **Show more options** → **Pin to taskbar**. Don't pin the WSL window the site runs in: that pins a plain `wsl.exe`, which only opens a Linux prompt.
- The site runs in a **minimized WSL window**. Close that window to stop the site. Clicking the shortcut while it's running just opens the page.
- Scheduled emails and the daily Instagram check only happen while the site is running.
- **After code changes,** close the WSL window and click the shortcut again.

### Troubleshooting

| Symptom | Fix |
|---|---|
| Not sure what's missing | `refer setup-check` |
| `config.yaml not found` | Step 4. |
| `ANTHROPIC_API_KEY is missing` | Step 3. |
| Search finds people at the wrong company | Paste the company's LinkedIn page into **LinkedIn company URL** and search again; it's remembered. |
| "Gmail login expired" | Google Testing-mode logins last 7 days. Run the two `refer gmail-login` steps again. |
| Email credits show red | Out of Hunter/Apollo credits this month; add emails by hand under **No email found**. |
| LinkedIn shows a CAPTCHA or "unusual activity" | Stop for a day; lower `linkedin.max_profiles_per_day` and `profiles_to_verify`. |
| Profile checks come back empty | LinkedIn changed its pages: run `refer check --profile https://www.linkedin.com/in/me/` and see `referral/profiles.py`. |

## Commands

| Command | What it does |
|---|---|
| `refer setup-check` | Checks keys, config, resume, LinkedIn login, browser and Gmail, and says how to fix anything missing. |
| `refer web [--port 8000]` | Starts the website (see above). |
| `refer gmail-login` then `refer gmail-login --finish "<url>"` | Connects your Gmail for sending (see Gmail setup). |
| `refer ig-login` | Opens a browser to log in to Instagram (use a spare account) for the story jobs. Saves `ig_session.json`. |
| `refer ig-fetch [--force]` | Reads today's stories and adds the jobs they link to. At most once a day unless `--force`. |
| `refer login` | Opens a visible browser to log in to LinkedIn (2FA included, 5 min limit). Saves cookies to `session.json`. |
| `refer check` | Confirms the saved session is still logged in. |
| `refer check --profile <url>` | Also scrapes one profile (its experience + education pages) and prints the name, education and top 3 jobs. Use it to test that scraping works. `https://www.linkedin.com/in/me/` tests on your own profile. |
| `refer check --job <linkedin job url>` | Also scrapes one LinkedIn job posting. |
| `refer find <job url> [--keywords K] [--pages N] [--verify N] [--company-url U]` | Command-line version of the website's search. Parses the job, searches for alumni, ranks them, scrapes the top `--verify` profiles (default `linkedin.profiles_to_verify`) to confirm the school and graduation year, shows a ranked table, and asks who to contact (`1,3,5` or `1-4`). Saves the job, all candidates and your picks to `runs/<company>-<job id>.json`. Scraped profiles are cached for 30 days, so re-running costs only the search pages. |
| `refer search <job url> [--keywords K] [--pages N] [--company-url U]` | Parses the job, finds the company on LinkedIn, and lists alumni of your school who currently work there and match the keywords (default: first `target_roles` entry). Shows name, connection degree, headline, location, profile URL and LinkedIn's one-line summary. Use `--company-url` if the name lookup picks the wrong company. |
| `refer job <url> [--full]` | Parses a job posting and prints the company, title, department/team, location and description. Tracking parameters (`utm_*`, `fbclid`, `ref`, `src`, …) are removed; other query parameters (e.g. IBM's `jobId`) are kept. |

## Configuration (`config.yaml`)

| Key | Meaning |
|---|---|
| `me.*` | Your name, school, class year, major and resume path. Fills the email template. |
| `me.application_email` | The address you apply to jobs with. The email template mentions it so referrers enter it on the referral form, where companies match referrals to applications. |
| `me.linkedin_school_id` | Your school's LinkedIn ID for the `schoolFilter` (Cornell = `3523`, from the school page's alumni link). |
| `target_roles` | Role keywords used to rank alumni by how relevant they are. |
| `linkedin.session_path` | Where the login cookies are stored (gitignored). |
| `linkedin.max_profiles_per_run` / `max_profiles_per_day` | Scraping limits so your account doesn't get flagged. |
| `linkedin.delay_seconds` | Range of random pauses between profile scrapes. |
| `linkedin.search_pages` | Result pages read per search (10 people each). |
| `linkedin.profiles_to_verify` | How many top-ranked candidates `find` scrapes (2 page loads each). Limited by `max_profiles_per_run` and by what's left of `max_profiles_per_day` (tracked in `.cache/scrape_log.json`). |
| `linkedin.headless` | `false` shows the browser while it scrapes (useful for debugging). |
| `hunter.monthly_credit_limit` | Hard monthly cap on Hunter credits (default 50). |
| `apollo.monthly_credit_limit` | Your Apollo monthly allowance (free plan: 75). |
| `apollo.reset_day` | Day of the month Apollo renews your credits (see its billing page); default 1. |
| `models.compose` | Claude model that writes the email blurb (`claude-sonnet-5-5`). |
| `models.extract` | Claude model that turns scraped profile text into structured experience/education (`claude-haiku-5-5`). |

## Layout

```
referral/
  cli.py       # Typer CLI (`refer` entry point)
  config.py    # loads config.yaml + .env
  linkedin.py  # session create/load/check on top of linkedin_scraper; fixes its rate-limit false positive
  profiles.py  # profile scraper: details/experience + details/education text -> Claude -> Profile
  llm.py       # Anthropic client (sends the workspace header)
  jobs.py      # job URL -> JobPosting (ATS APIs, LinkedIn, or fetch + Claude)
  search.py    # company -> LinkedIn company IDs; alumni people search + result parsing
  rank.py      # candidate scoring (card pass + profile pass) with reasons
  pipeline.py  # find flow shared by CLI and web: company → search → rank → verify
  web.py       # FastAPI app for the website (local only), incl. the search queue
  instagram.py # jobs from Instagram stories (login, daily check, Claude reads each story)
  simplify.py  # open internships from SimplifyJobs' listings.json, for the "Jobs to apply to" column
  static/index.html  # the single-page UI
  emails.py    # Hunter email finder + company-format guess verified by Hunter
  compose.py   # template.md + Claude → draft email
  gmail.py     # Gmail OAuth (send-only) + sending with the resume attached
template.md    # YOUR email template (subject, body, instructions for Claude)
  store.py     # JSON storage: profile cache, daily scrape counter, runs/
tests/         # offline tests: `.venv/bin/pytest tests`
```

## How profile scraping works

`linkedin_scraper`'s `PersonScraper` no longer works with LinkedIn's current pages: there's no `<h1>`, and the class names are scrambled, so it returns empty profiles. `referral/profiles.py` replaces it:

1. It opens `<profile>/details/experience/` and `<profile>/details/education/`, with a random 3–6s pause between them.
2. It keeps the visible text of `<main>` and cuts off the sidebar/footer (from "Profile language" onward).
3. It sends that text to `models.extract` with structured output and gets back `Experience` / `Education` records.

That's 2 page loads per person (PersonScraper made ~7), and it keeps working when LinkedIn changes its markup.

`referral/linkedin.py` also replaces the library's `detect_rate_limit`. The original searched the page's *full* text, including hidden script data, for phrases like "try again later". Every LinkedIn profile has a hidden "Something went wrong. Please try again later." string, so every scrape failed. The replacement checks only visible text, and keeps the checkpoint and CAPTCHA checks.

## How job parsing works

`referral/jobs.py` chooses a source based on the URL:

| URL | Source | Notes |
|---|---|---|
| `jobs.smartrecruiters.com/<company>/<id>` | `api.smartrecruiters.com/v1/companies/<company>/postings/<id>` | Public. Has the company's display name and department. |
| `job-boards.greenhouse.io/<board>/jobs/<id>` (or `boards.`) | `boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>?content=true` | Public. Has `company_name` and departments. |
| `jobs.lever.co/<company>/<uuid>` | `api.lever.co/v0/postings/<company>/<uuid>` | Public. No display name, so the company name comes from the URL slug (e.g. "Palantir"). |
| `jobs.ashbyhq.com/<org>/<uuid>` | `api.ashbyhq.com/posting-api/job-board/<org>` (filtered by id) | Public. Same slug-derived name (e.g. "Openai"). |
| `<tenant>.wdN.myworkdayjobs.com/[en-US/]<site>/job/<path>` | `https://<host>/wday/cxs/<tenant>/<site>/job/<path>` (Workday's JSON API) | Public. Company from `hiringOrganization`, with legal suffixes removed ("Zillow Group, Inc." → "Zillow Group"). The page itself is JavaScript-only, so this API is the only way to read it. |
| `linkedin.com/jobs/...` | `linkedin_scraper` `JobScraper` | Needs `session.json`. |
| `simplify.jobs/c/...` | rejected | That's a Simplify company page, not a job. Use the job's application link. |
| anything else, with a schema.org `JobPosting` block | that JSON-LD block (exact, free) | Many career sites embed it for Google Jobs; often the only place the description is (e.g. jobs.aon.com). |
| anything else | page fetched (falls back to headless Chromium when a site returns a bot-check or an empty page, e.g. IBM), converted to text, structured by `models.extract` | Claude also judges whether the page is really a job posting. If it isn't sure (login walls, "applied jobs" pages), the search **continues with a ⚠ warning** in the progress log and on the search's header, so you can check the title before sending. It only stops if no company/title can be read. |

## How alumni search works

`referral/search.py` (`linkedin_scraper` has no people search):

1. **Find the company.** Postings often use a legal entity name ("AFMIC American Family Mutual Insurance Company, S.I.", "Amazon.com Services LLC"). Legal suffixes are removed, and the first time a company is seen, Claude (`models.extract`, about $0.0005) gives the name its main LinkedIn page uses ("American Family Insurance", "Amazon"). LinkedIn's company search is tried with that name, then the posting's name, then the posting's name without generic words ("Zillow Group" → "Zillow"; ".com" and "Services" are dropped too). Candidates are ranked: exact name matches by followers (so the real "Allstate" beats an agent's page also called "Allstate Insurance"), then pages whose name contains the company's first word, by followers. Unrelated pages are never used. The search stops early at an exact match with at least 10,000 followers. If the best page has no "see all employees" link, the next candidate is tried (up to 3). The result is cached in `.cache/companies.json`, and the site logs the page it chose. If it's wrong, rerun with `refer find <job url> --company-url <right page>`; the correction is remembered for that company name.
2. **Get the company IDs.** It opens the company page and reads its "see all employees" link, a people search filtered by `currentCompany` only. That gives LinkedIn's own ID list, affiliated pages included (LinkedIn → `1337`, `39939`, `2587638`). The result is cached in `.cache/companies.json`, so each company costs those 2 page loads only once.
3. **Search.** It opens `/search/results/people/?currentCompany=[…]&schoolFilter=["3523"]&keywords=…&page=N`. The filter lists must be compact JSON: with `json.dumps`'s default `", "` spacing, LinkedIn silently drops the filter. The keyword is your first `target_roles` entry. If fewer than 5 people match it (e.g. no software engineers at an insurance company), it searches again with no keyword and adds everyone found; the ranking still puts relevant roles first.
4. **Read the cards.** Every result card has exactly one `• 2nd` / `3rd+` degree marker, so each card is found by climbing from a name link to the largest ancestor with one marker. Mutual-connection links (also `/in/` links) are skipped because the card text doesn't start with their name. Some name links wrap the whole card, so the name is the link text's first line.

LinkedIn's summary line sometimes says "You both *worked at* <school>" rather than "went to". The school filter still applies; that line just picks one thing you have in common. The profile scrape in the ranking step confirms the education.

## How ranking works

`referral/rank.py` scores in two passes and records a reason for every point, which the table's "Why" column shows.

**Card pass** (every search result, no extra page loads):

| Signal | Points |
|---|---|
| A `target_roles` entry appears in the headline or summary | +3 |
| Seniority, from the summary's current title (falls back to the headline): engineer/SWE +3, senior or manager +2, staff +1, principal 0, director/VP −1 | −1 to +3 |
| Recruiter / talent / sourcer | −1 |
| Graduation year in the card (e.g. "Cornell'22", built from `me.school`): ≤3 years ago +3, ≤7 +2, ≤12 +1; current student +1 | 0 to +3 |
| Posting's team name appears in their card | +2 |
| 1st / 2nd-degree connection | +2 / +1 |

**Profile pass** (top `profiles_to_verify` only):
- No entry for your school in their education: −10, marked ✗.
- Graduation year taken from that school entry's `end_year` if the card didn't show one.
- No current role at the company: −5.

Seniority counts so heavily because recent grads remember recruiting and are the most likely to say yes. Senior people get many more requests.

## Gmail setup (one time)

1. Go to https://console.cloud.google.com and create a project (any name, e.g. "referral-finder").
2. **APIs & Services → Library →** search "Gmail API" → **Enable**.
3. **Google Auth Platform** (formerly "OAuth consent screen") → **Get started**. Fill in an app name and your email, choose Audience **External**, and finish.
4. **Audience → Test users → Add users →** add the Gmail address you'll send from.
5. **Clients → Create client →** Application type **Desktop app** → **Create** → **Download JSON**. Save it as `referral-finder/credentials.json`.
6. Run `.venv/bin/refer gmail-login` and open the URL it prints. Approve; Google warns that the app isn't verified, but it's your own app, so click **Continue**. The browser then ends on a "site can't be reached" page at `localhost:8090/?state=…&code=…`, which is expected: nothing listens there. Copy that whole address and run `.venv/bin/refer gmail-login --finish "<address>"`. This writes `token.json`. (Two steps because a waiting local server didn't survive being run from Claude Code's `!` prompt, and WSL↔Windows localhost forwarding isn't reliable.)

Notes:
- Scopes are `gmail.send` plus your email address. The tool can't read your mail.
- While the app is in Google's **Testing** mode, the login expires after **7 days**. The website will say "Gmail login expired"; just run `refer gmail-login` again.
- Every real send is appended to `.data/sent.jsonl` (gitignored), which is how "never email the same person twice" works across searches.

## Finding emails

`referral/emails.py`, run from the **Find emails** button, only for selected people. It's never repeated for someone who already has a result.

1. **Hunter email finder** with full name, company (or its domain, once one lookup has revealed it) and LinkedIn handle. It costs **1 credit, only when an email is found**.
2. **Apollo people match** (`POST /api/v1/people/match`) for anyone Hunter misses, if `APOLLO_API_KEY` is in `.env`. It sends name, LinkedIn URL and company, with personal emails off. Apollo bills **1 credit per matched person, even if no email comes back** (it counts profile data as billable); nothing when there's no match (`match_confidence: none`). Matches with `match_confidence` low or none are rejected (likely the wrong person). Apollo often returns `extrapolated` addresses, which are its own format guess, not a known mailbox. These are used **without** verification (to save Hunter credits) and badged "unverified guess, may bounce". An occasional bounce is harmless at this volume; switch that person to the LinkedIn invite. `apollo.monthly_credit_limit` is enforced with a local counter in `.data/apollo_usage.json` that counts the way Apollo bills. Apollo's API can't report your balance; check **Settings → Billing and credits** on apollo.io and set the limit to your real allowance. Note: Apollo only gives free accounts API access if they **signed up with a work email**.
3. **Company-format guess** (**off by default**: `hunter.guess_emails: false`) for anyone still missing: it learns the format from addresses already found at the company (e.g. `jdoe@acme.com` for Jane Doe → `{f}{last}`), then tries that format and common ones (up to 3), checking each with Hunter's email verifier at **½ credit per guess**. A guess is only kept if the verifier says `valid`. Nothing is ever sent to an address that wasn't found or verified, unless you type it in yourself.

Each person's result records which methods were `tried`, so adding a method later (e.g. Apollo) lets **Find emails** retry earlier misses with just that method. A quota or auth error from a provider stops calls to it for the rest of the click.

Hunter's free plan has **50 credits a month, shared by searches and verifications**. `hunter.monthly_credit_limit` in `config.yaml` (default 50) is a hard cap: before every paid call the tool reads your usage from Hunter's free account endpoint and refuses any call that would go over. Credits left and the reset date show next to **Find emails**.

## Writing drafts

`referral/compose.py`, run from **Draft emails** / **Regenerate**.

- `template.md` has three sections: `# Subject`, `# Body` and `# Instructions`. `<!-- comments -->` and any other sections (e.g. an old `# Invitation`) are ignored.
- Placeholders are filled in exactly: `{first_name} {my_name} {application_email} {major} {class_year} {class_year_short} {school} {job_title} {short_title} {company} {job_link}`.
- Claude (`models.compose`, Sonnet 5.5) writes only `{personal_line}` and `{blurb}`, plus `{short_title}` and the company's proper display name. It follows `# Instructions` and gets the job description, the recipient's card and scraped profile, and your resume PDF.
- LinkedIn's "You both went to / worked at …" sentence is removed from the recipient summary first, because it's written from your point of view and is often wrong ("worked at Cornell").
- The request uses Sonnet 5.5 with the `fallbacks: "default"` server-side refusal fallback (beta `server-side-fallback-2026-07-01`). A refusal shows as an error.

## Costs

Every Claude call is logged to `.data/claude_usage.jsonl` (task, model, tokens, USD). Measured per call:

| Call | Model | Tokens (in / out) | Cost |
|---|---|---|---|
| Draft one email (resume PDF + job + profile) | Sonnet 5.5 | ~7,000 / ~300 | ~$0.017 |
| Structure one scraped profile | Haiku 5.5 | ~3,500 / ~1,400 | ~$0.001 |
| Read a generic job page | Haiku 5.5 | a few thousand in | ~$0.001 |

A typical job (8 profile checks + 3 drafts) costs about **$0.06** in Claude. Gmail API, Google Cloud and LinkedIn are free. Hunter (50 credits/month free) is the scarce resource; Apollo is the same.

## Never committed
`.env`, `session.json`, `credentials.json`, `token.json`, `*.pdf`, `*.db`, `.cache/` (company IDs, profiles, scrape log), `runs/`, `.data/` (sent-email log) (see `.gitignore`).

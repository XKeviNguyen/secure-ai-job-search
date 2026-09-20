<p align="center">
  <img src="assets/mascot/pip_flight_loop.gif" alt="Pip, the courier bird" width="200">
</p>

# Secure AI Job Search

*An AI-assisted job search framework that keeps your private data private.*

Secure AI Job Search runs on your machine and helps you search, evaluate, rank,
and prepare job applications - including login-gated job portals - without ever
handing your credentials, CV, or personal data to a model or a third party.

> Based on and substantially extended from
> [Mads Lorentzen's ai-job-search](https://github.com/MadsLorentzen/ai-job-search),
> licensed under the MIT License. See [NOTICE.md](NOTICE.md).

## What it does

- **Searches** job portals through portable CLI skills (LinkedIn, freehire, and
  regional boards) and ranks the matches against your profile.
- **Evaluates fit** with an eligibility gate, a language gate, configurable
  location/commute rules, and five scored dimensions.
- **Routes application documents** by market: Japanese-convention employers get
  separate **履歴書** and **職務経歴書**; English/international employers get one
  English resume.
- **Reviews drafts independently**: a fresh-context, read-only reviewer runs
  after drafting and before rendering.
- **Validates output**: page-by-page render inspection for Japanese DOCX and
  ATS text-layer/keyword QA for English resumes.
- **Prepares portal applications safely**, then stops before the irreversible
  submit.

## Workflow

```
search -> evaluate -> approve -> route documents -> independent review
       -> render / ATS QA -> portal preparation -> human final submission
```

Nothing is ever submitted automatically. Every application ends in
`drafted_for_review` (or `READY_FOR_FINAL_SUBMISSION`) and waits for you.

## Supported output routes

| Route | Outputs |
|---|---|
| Japanese-convention employer | separate 履歴書 and 職務経歴書 (copied DOCX + rendered PDF) |
| English / international employer | one tailored English resume (source + PDF) |

Routing lives in
[`.claude/skills/job-application-assistant/10-application-output-routing.md`](.claude/skills/job-application-assistant/10-application-output-routing.md).
A cover letter is ancillary, created only when a posting requires it.

## Authenticated portals

Some employers post only behind a login. The authenticated layer is deliberately
narrow:

```
AI agent -> portal-operator subagent -> narrow portal CLI
         -> loopback CDP (127.0.0.1) -> dedicated Chrome profile
         -> your password manager (e.g. RoboForm)
```

- The browser uses a **dedicated profile** at
  `~/.local/share/ai-job-search/chrome-profile`, never your personal Chrome
  profile.
- The agent drives it only through `tools/job_browser.py` and `tools/portal.py`.
  There is **no raw Chrome DevTools MCP**, so click/fill/upload/navigate cannot
  bypass the guardrails.
- The portal-operator subagent cannot edit files, read arbitrary files, run
  arbitrary shell, or search the web.
- Exact hostname validation keeps the authenticated session on the approved
  portal hosts; external pages are public-research targets only.

### Why passwords never belong in `.env`, Git, prompts, or repo files

A private repository does **not** make a committed password safe, and
`.gitignore` is not a credential manager. This framework therefore never asks
for a password and never stores one:

- You install and unlock your password manager manually inside the dedicated
  browser.
- The agent waits for autofill and may click the portal's normal **Login**
  button; it never reads the username or password field values.
- No password is passed as a CLI argument or environment variable, and no
  cookie, session token, or CSRF value is logged or persisted.

### Human challenges (Cloudflare / CAPTCHA / MFA / passkey)

These remain human actions. When one appears:

1. The framework raises a **sanitized desktop notification**
   ("Job Search — Action Required") and brings the dedicated browser forward.
2. You complete the challenge by hand.
3. The workflow **detects clearance and resumes automatically** - no terminal
   `continue` needed. On timeout it returns `USER_ACTION_REQUIRED_TIMEOUT` and
   leaves the browser open.

### Final-submit safety

The final, irreversible **Apply/Submit** is never clicked. When the application
reaches its final confirmation the workflow returns
`READY_FOR_FINAL_SUBMISSION`, shows exactly what would be sent, and stops. You
submit manually.

## Privacy model

- The repo ships as a **public, unonboarded template**: no candidate data.
- Private CV/DOCX masters go under the gitignored `documents/cv/`.
- Per-application manifests, drafts, PDFs, postings, and outcomes live under the
  gitignored `documents/applications/` and `documents/postings/`.
- Your local candidate preferences live in the gitignored
  `config/candidate-preferences.yaml`.
- `tools/privacy_scan.py` fails closed on candidate/private fingerprints, secret
  patterns, private-key headers, token patterns, password assignments,
  `.env`/cookie files, private-document extensions, browser-profile databases,
  and accidentally tracked application state.

## Security model

- Dedicated browser profile only; personal Chrome is never used.
- CDP binds to loopback only; no `0.0.0.0`, no TLS bypass, no
  `--disable-web-security`, no `--no-sandbox`, no remote-allow-origins.
- No raw browser MCP; the portal-operator sandbox is the only path.
- Exact hostname and manifest-bound upload checks.
- No CAPTCHA/MFA bypass; no arbitrary click API.
- Source documents are immutable and hashed.

## Installation (Ubuntu)

Prerequisites: Python 3.10+, Git, Google Chrome, a LaTeX distribution with
`lualatex` and `xelatex`, and [Bun](https://bun.sh) for the portal CLI skills.

```bash
git clone https://github.com/XKeviNguyen/secure-ai-job-search.git
cd secure-ai-job-search

# Portal CLI dependencies
for tool in jobbank-search jobdanmark-search jobindex-search jobnet-search linkedin-search freehire-search; do
  (cd .agents/skills/$tool/cli && bun install)
done

# Pinned Python dependency for the authenticated portal layer
python3 -m pip install -r tools/requirements.txt
```

## Onboarding

The repository is a framework **before onboarding**. Run `/setup` in your agent
runtime to generate your private profile from your CV and documents, or fill in:

- `CLAUDE.md` Candidate Profile section
- `.claude/skills/job-application-assistant/01-candidate-profile.md`
- `config/candidate-preferences.yaml` (copy from the `.example`)

Private CVs go under the gitignored `documents/cv/`. See
[`documents/README.md`](documents/README.md) and
[`SETUP.md`](SETUP.md).

## Configuration

Location, commute, salary, work authorization, languages, and career tracks are
candidate configuration, not product defaults. See
[`config/candidate-preferences.example.yaml`](config/candidate-preferences.example.yaml).
The example uses clearly fictional values.

## Commands

- `/setup` - onboard the profile
- `/scrape` - search portals; `/rank` - rank a scrape batch
- `/apply <url|file|text>` - evaluate, route, draft, review, render, stop
- `/portal` - read and safely prepare authenticated portal applications
- `/outcome`, `/interview`, `/gmail-sync`, `/notion-sync`, `/html-report`
- `/add-template`, `/add-portal`, `/upskill`, `/expand`, `/reset`

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 tools/lint_skills.py
python3 tools/security_guards.py
python3 tools/check_framework_version.py
python3 tools/privacy_scan.py
```

A fresh clone passes the full suite before onboarding. Authenticated-portal
tests use synthetic fixtures only and never real credentials.

## Attribution and license

MIT License. Based on and substantially extended from
[Mads Lorentzen's ai-job-search](https://github.com/MadsLorentzen/ai-job-search).
This distribution adds application routing, document-integrity checks, the
independent reviewer workflow, authenticated portals, RoboForm-safe browser
isolation, human-challenge alerts, application-attempt identity, and
security/privacy guards - none of which were authored upstream. See
[NOTICE.md](NOTICE.md).

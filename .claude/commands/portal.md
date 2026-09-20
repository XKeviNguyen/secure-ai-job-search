# /portal - Authenticated Portal Reading and Preparation

Read or prepare a job on an authenticated portal (v1: CareerCross) through the
dedicated Job Search browser. Follow `11-authenticated-portals.md` as the
canonical security contract. This command never handles credentials and never
performs a final submission.

## Step 0: Preflight

Under OpenCode, dispatch the scoped `portal-operator` subagent
(`.opencode/agents/portal-operator.md`) for every authenticated browser step.
Do not use any raw browser / Chrome DevTools MCP tool: the operator's only
permitted commands are `python tools/job_browser.py ...` and
`python tools/portal.py ...`.

1. Run `python tools/job_browser.py doctor`. If it fails, report the exact
   failing check and stop.
2. Run `python tools/job_browser.py start`, then `status`. Confirm the CDP
   endpoint is reachable and the dedicated profile is in use.
3. If the dedicated browser or the pinned CDP dependency
   (`tools/requirements.txt`) is unavailable, report `USER_ACTION_REQUIRED` and
   tell the user what to start or install.

## Step 1: Read a posting (no application)

Run:

```bash
python tools/portal.py read --portal careercross --url "<job-url>" --json
```

- If the result state is `AUTH_REQUIRED`, run the safe login command:

  ```bash
  python tools/portal.py login --portal careercross --json
  ```

  It navigates only to the canonical CareerCross login URL, waits for RoboForm
  autofill, and clicks only the login form's normal control on an approved
  host. It reports only booleans and never returns field values. If fields stay
  empty or RoboForm is locked, return `USER_ACTION_REQUIRED`: "Unlock/use
  RoboForm in the dedicated Job Search browser, then continue." Do not ask for a
  password.
- If the result state is `USER_ACTION_REQUIRED` (CAPTCHA, MFA, passkey, e-mail
  verification, or locked RoboForm), run:

  ```bash
  python tools/portal.py wait-user-action --portal careercross --timeout 300 --json
  ```

  This raises a sanitized desktop notification, brings the dedicated browser to
  the front, and polls the minimum non-sensitive signals until the human
  finishes the challenge, then resumes automatically. Never solve or bypass a
  challenge yourself. On timeout it returns `USER_ACTION_REQUIRED_TIMEOUT` and
  leaves the browser open.
- On `PUBLIC` or `AUTHENTICATED`, continue.

`open --url` is available when only the page state is needed.

Report only the whitelisted job fields. Never echo account page content.

## Step 2: Hand off to `/apply`

Save the extracted posting verbatim to
`documents/postings/<Company> - <Role>.txt` (gitignored) and run `/apply` on
that path. Route, fit evaluation, approval, drafting, the independent reviewer,
rendering, and ATS QA are `/apply`'s existing steps — do not duplicate them.

## Step 3: Prepare the application (explicit approval only)

Only after the documents pass `/apply` QA and the user explicitly says to
prepare the application:

```bash
python tools/portal.py documents --manifest "<attempt>/application-output.json" --json
```

1. `python tools/portal.py form-snapshot --url "<application-url>" --json` —
   returns safe field metadata only (identifier, label, type, required,
   maxlength, options). It never returns a password, hidden, or token value.
2. Draft the answers locally from the verified profile into a JSON
   `{"fields": {...}}` file, then
   `python tools/portal.py fill --url "<application-url>" --answers <file> --json`.
   It refuses password/hidden/token fields, enforces maxlength and select/radio
   options, and returns `USER_ACTION_REQUIRED` for an unknown required field or
   a material fact (salary / visa / availability) that is not explicitly
   `confirmed_sensitive`.
3. `python tools/portal.py upload --url "<application-url>" --manifest
   "<attempt>/application-output.json" --kind <kind> --json`. Files are resolved
   from that exact attempt and hashed; no file path is accepted from the caller,
   and cross-attempt files are refused.
4. `python tools/portal.py continue --json` advances one clearly non-final
   control only. Final or ambiguous controls return
   `READY_FOR_FINAL_SUBMISSION` / `SUBMISSION_BLOCKED` and are not clicked.
5. Do not automate RoboForm Identity autofill in v1.
6. Present the summary (portal, company, role, exact uploaded documents +
   hashes, answer summary, declarations, unresolved warnings) and stop. The user
   submits manually.

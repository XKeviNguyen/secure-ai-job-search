# Search Queries for Job Scraper

<!-- PUBLIC TEMPLATE. Configured locally by /setup from your career tracks,
     preferred locations, and languages. Values below are generic examples. -->

## Installed portal CLIs (primary for `/scrape`)

`/scrape` discovers portal skills under `.agents/skills/*/SKILL.md`. The shipped
skills cover jobindex, jobnet, jobbank, jobdanmark, LinkedIn, and freehire. Add
your own with `/add-portal`.

The `site:` query templates in this file are the **WebSearch fallback** — for
portals without a CLI, company career pages, or when a CLI fails.

**Language scope:** query each configured career track with appropriate
language variants for the target market. A language in an ad is not by itself a
role requirement. A required undeclared working language FAILs; a higher
declared-language bar is FLAGged per `04-job-evaluation.md`.

## Search Sites

List the boards for your market here, for example:

- **International / remote:** LinkedIn Jobs (installed CLI), freehire.me (installed CLI)
- **Your local market:** add employer career pages and regional boards

Before adding any automated portal skill, check terms, technical feasibility and
applicable legal conditions.

## Query Categories

Organize queries **by function, not job title**. The same work carries different
titles across companies; list several plausible titles per category. Read
`config/candidate-preferences.yaml` for `career_tracks` and
`preferred_locations` — do not hard-code a city or track here.

### Priority 1A: [CAREER_TRACK]

Title families: [TITLE_VARIANT], [TITLE_VARIANT], [TITLE_VARIANT].

```
site:example.com "[ROLE_KEYWORD]" "[PREFERRED_LOCATION]"
"[ROLE_KEYWORD]" remote "[LANGUAGE]"
```

### Priority 1B: [CAREER_TRACK]

Title families: [TITLE_VARIANT], [TITLE_VARIANT], [TITLE_VARIANT].

```
site:example.com "[ROLE_KEYWORD]" "[PREFERRED_LOCATION]"
```

### Priority 2: [CAREER_TRACK]

Title families: [TITLE_VARIANT], [TITLE_VARIANT].

```
site:example.com "[ROLE_KEYWORD]" "[PREFERRED_LOCATION]"
```

**Distinctive searchable terms:** [SKILL], [SKILL], [LANGUAGE] [LANGUAGE_LEVEL],
[CERTIFICATION]. Select three to five relevant terms per query; do not require
all in one posting.

## Location Filter

Read `preferred_locations`, `nearby_regions`, and `max_commute_minutes` from
`config/candidate-preferences.yaml`:

- **Primary:** a configured preferred location when the one-way commute is
  practical and within `max_commute_minutes`.
- **Nearby:** a configured nearby region only when its actual station/location
  is realistically within the configured commute. Do not approve a whole region
  by adjacency.
- **Borderline:** a commute above the configured maximum, or an unknown
  workplace/station. FLAG and retain for review; do not assume.
- **Too far:** a confirmed workplace clearly beyond the configured commute, or a
  region the candidate has not opted into.
- **International remote:** a role that can engage the candidate from their
  location; evaluate hiring and contract terms separately.

## Work Authorization, Employment and Salary

Read `work_authorization` and `salary` from `config/candidate-preferences.yaml`.

- If `requires_sponsorship` is true, explicit no-sponsorship or
  citizens/PR-only conditions FAIL; silence is FLAG for role-specific research.
- International remote roles must be capable of engaging the candidate in their
  location.
- Salary is a ranking signal, never an automatic veto. Commission-only pay FAILs.
- Flag schedule friction; do not reject automatically unless a mandatory
  incompatible schedule is confirmed.

## Language Filter

Apply `04-job-evaluation.md`'s Language Gate against the Languages table in
`01-candidate-profile.md`. A required, wholly undeclared language FAILs; a
declared language required above the demonstrated level FLAGs for human
judgment. The language of the ad is not by itself the language required on the
job.

## Date Filter

Only include jobs posted within the last 14 days, or with an application
deadline that has not yet passed. If a posting date cannot be determined,
include it but flag as "date unknown".

## Adapting Queries

If the user specifies a focus area, select queries from the matching category
and generate 2-3 custom queries for that focus.

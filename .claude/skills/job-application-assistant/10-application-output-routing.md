---
framework_version: 1.2.0
---

# Application Output Routing

This file is the canonical document-family router for `/apply`. It decides which application documents are created after fit evaluation and explicit user approval. It does not decide whether a role is a good fit; `04-job-evaluation.md` owns that decision.

## Routing Decision

Use the employer market and requested application format, not only the language of the advertisement:

| Situation | Route | Required outputs |
|---|---|---|
| Japanese employer or Japan-based role using Japanese application conventions | `japanese` | Separate 履歴書 and 職務経歴書, each as an edited DOCX copy plus final PDF |
| English-language international employer, including remote roles able to hire from Vietnam | `english_international` | One tailored English resume source plus final PDF |
| Posting explicitly names a required format that conflicts with the default above | Follow the explicit employer format after telling the user | Never silently substitute another document family |
| Employer market or expected format is genuinely ambiguous | Stop and ask | Do not guess from the posting language alone |

A Japan-based posting written in English still uses the Japanese route unless the employer explicitly requests an English resume. An international posting mentioning Japan or Japanese-language ability does not become a Japanese-format application by that fact alone.

Cover letters and portal free-text answers are ancillary documents, not routing defaults. Create them only when the posting requires them or the user explicitly requests them; they never replace either Japanese document or the English resume.

## Deterministic Paths and Re-application Attempts

Derive the base `<application_id>` from company plus role using the normalization rule in `documents/README.md`. Run the path planner rather than inventing names:

```bash
python tools/application_output.py plan --market <japanese|international> --company "<company>" --role "<role>"
```

The first application keeps the canonical base ID. If that application directory already exists, the planner and preparer select the lowest available numbered attempt (`<application_id>_2`, then `_3`, and so on). Any existing directory counts as occupied, including a legacy or incomplete archive; never reuse it or overwrite its posting, manifest, drafts, submitted files, or outcome. `prepare` performs the authoritative allocation and returns the exact attempt ID and manifest path. Use that returned manifest everywhere downstream rather than recalculating the base name.

Each attempt uses:

```text
documents/applications/<application_id>/
├── application-output.json
├── job_posting.md
└── drafts/
    ├── japanese/
    │   ├── <candidate_slug>_<application_id>_rirekisho.docx
    │   ├── <candidate_slug>_<application_id>_rirekisho.pdf
    │   ├── <candidate_slug>_<application_id>_shokumukeirekisho.docx
    │   └── <candidate_slug>_<application_id>_shokumukeirekisho.pdf
    └── english/
        ├── <candidate_slug>_<application_id>_resume.<active extension>
        └── <candidate_slug>_<application_id>_resume.pdf
```

Only one route folder is populated for a normal application. The entire application directory is personal data and remains gitignored.

`application-output.json` is the authoritative list of document kinds and paths. Legacy tracker fields remain a compatibility index: `cv_file` points to the English resume source or Japanese 履歴書 DOCX; `cover_letter_file` is blank unless an actual cover letter exists. Never put 職務経歴書 in `cover_letter_file`. Consumers that need every application document read the manifest.

The tracker row for every new routed attempt must include the exact `application_manifest=<relative path returned by prepare>` pointer. `/outcome` and `/interview` resolve that pointer and its containing attempt directory; they do not guess the base company-role folder. Base-folder derivation is a legacy fallback only for tracker rows that predate manifest pointers.

## Japanese Source Resolution

The private masters live under `documents/cv/` and are never committed. Resolve exactly one source whose filename identifies 履歴書 and exactly one source whose filename identifies 職務経歴書. Exclude files inside `documents/applications/`. If either match is missing or ambiguous, stop and ask the user to identify the source files.

Prepare byte-identical working copies with:

```bash
python tools/application_output.py prepare \
  --market japanese \
  --company "<company>" \
  --role "<role>" \
  --source "rirekisho=<absolute source DOCX>" \
  --source "shokumukeirekisho=<absolute source DOCX>"
```

The command records SHA-256 hashes for both originals and allocates a new numbered attempt rather than overwriting any existing application. Edit only the returned manifest's `working_path` files. Immediately before the first DOCX authoring edit, follow the official documents skill's artifact-operation marker requirement with operation kind `edit`, expected output count `2`, and output format `docx`.

Use the documents skill's retained-template workflow:

1. Distill each original DOCX as a template and record its SHA-256, page/section system, edit slots, hyperlinks, and preserve-only package parts in task-local temporary artifacts.
2. Patch the prepared copy in place with minimal OOXML edits. Preserve the original styles, tables, images, relationships, hyperlinks, page geometry, and unrelated package parts.
3. Tailor only supported content. 履歴書 normally changes the current date, 志望動機, 自己PR, 本人希望, and role-relevant emphasis when those slots exist. 職務経歴書 normally changes the professional summary, ordering/emphasis of supported experience, project/skill selection, and role-specific wording.
4. Use `01-candidate-profile.md`, `CLAUDE.md`, and the verified English master reference only as factual sources. Existing tailored drafts are phrasing references, not evidence.
5. Do not add Japan-specific private fields to an English resume. In 履歴書, preserve or edit private fields only when that Japanese document or application requires them.
6. Render both copies with the documents skill's packaged `render_docx.py --emit_pdf`, using the workspace dependency runtime and bundled LibreOffice. Never invoke `/usr/bin/libreoffice` or a desktop LibreOffice installation.
7. Inspect every rendered PNG at 100% zoom, compare reference and final renders, and iterate until both documents preserve the source layout without clipping, overlap, broken tables, missing Japanese glyphs, unexpected page-count changes, or hyperlink loss.
8. Run `verify-sources` against the manifest after all edits and confirm both private masters still match their recorded hashes.

## English Source Resolution

Resolve the active English CV template from `05-cv-templates.md` exactly as `/add-template` documents it. With no active override, use `cv/main_example.tex`. With an active override, use its template skeleton, source extension, declared compile command, companion files, fonts, style rules, and page limit.

Prepare the working source with:

```bash
python tools/application_output.py prepare \
  --market international \
  --company "<company>" \
  --role "<role>" \
  --source "english_resume=<absolute active template source>"
```

Copy any companion assets required by an active custom template into the draft folder or reference them by the manifest's documented relative paths. Tailor the copied source using `05-cv-templates.md`; never edit the template skeleton or `cv/main_example.tex`.

Compile with the active template's declared command, substituting the manifest's working basename and output directory. For the stock template use `lualatex`. Visually inspect every PDF page, enforce the active page limit, then run `tools/verify_pdf.py` for text-layer, contact-detail, reading-order, date, and JD-keyword QA. Delete extracted text and build intermediates after verification; retain only the resume source and final PDF.

## Independent Review Boundary

Drafting and rendering are separated by an independent reviewer with a fresh context. After the drafter creates the working copies and before any render, ATS, or visual check:

- Japanese route: the reviewer reviews both the 履歴書 and the 職務経歴書 working copies as a pair.
- English/international route: the reviewer reviews the tailored English resume working copy.

The reviewer returns proposed edits only. It must never modify `documents/cv/*`, original templates, or master source files, and it must never render, upload, or submit an application. The drafter applies only grounded and truthful improvements before rendering.

Use the isolated reviewer mechanism available in the current runtime: under OpenCode, the project `application-reviewer` subagent; on other runtimes, their equivalent fresh-context, read-only reviewer. This workflow is runtime-neutral and must not depend on any single provider.

## Review and Submission Boundary

Both routes end in `state: drafted_for_review`. Present the rendered, verified outputs and stop for the user's review. Never open an employer portal, upload a file, send an email, or submit an application unless the user later gives an explicit submission instruction. Even then, follow the relevant portal/application workflow and show the exact files selected before any irreversible action.

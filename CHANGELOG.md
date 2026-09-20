# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 1.0.0

First public release of the Secure AI Job Search framework, published as a
clean-room distribution derived from Mads Lorentzen's `ai-job-search`
(MIT licensed) with substantial security, routing, and portal extensions.

### Added

- **Multi-market application routing**: Japanese-convention employers receive
  separate 履歴書 and 職務経歴書 outputs; English/international employers receive
  an English resume. The route is decided in one canonical table.
- **Independent reviewer workflow**: a fresh-context, read-only reviewer runs
  after drafting and before rendering, on the Japanese document pair or the
  English resume, and returns proposed edits only.
- **Document integrity**: immutable SHA-256 source hashes, per-application
  manifests, and numbered re-application attempts (`base`, `_2`, `_3`, ...).
- **Render and ATS validation**: page-by-page visual inspection for Japanese
  DOCX and text-layer/keyword QA for English PDFs.
- **Secure authenticated portals**: a provider-neutral portal layer with exact
  hostname validation, minimized job extraction, a dedicated Chrome profile,
  loopback-only CDP, a scoped portal-operator sandbox, and no raw browser MCP.
- **RoboForm-safe authentication boundary**: no vault access, no password
  handling, no credential persistence.
- **Human-challenge handling**: Cloudflare/CAPTCHA/MFA/passkey challenges raise
  a sanitized desktop alert and auto-resume after the human completes them.
- **Safe application preparation**: form snapshots, validated fills,
  manifest-bound uploads, and a hard block on the irreversible final submit.
- **Privacy and security guards**: a privacy scanner, secret scanning,
  gitignore/personal-data guards, and CI checks.

### Attribution

Based on and substantially extended from
[Mads Lorentzen's ai-job-search](https://github.com/MadsLorentzen/ai-job-search),
licensed under the MIT License. See [NOTICE.md](NOTICE.md).

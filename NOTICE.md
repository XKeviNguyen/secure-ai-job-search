# Notice

This project is **based on and substantially extended from**
[Mads Lorentzen's ai-job-search](https://github.com/MadsLorentzen/ai-job-search),
which is licensed under the MIT License (see [LICENSE](LICENSE), which retains
the original `Copyright (c) 2026 Mads Lorentzen` notice).

The upstream project is a language- and country-agnostic job-application
framework. This public distribution adds substantial extensions, including:

- market-aware application document routing (Japanese 履歴書 + 職務経歴書 vs.
  English resume)
- immutable source-document hashes and per-application manifests
- application-attempt identity and collision-safe re-application attempts
- an independent, fresh-context reviewer step before rendering
- ATS/text-layer and rendered-visual verification workflow
- a secure authenticated-portal architecture (dedicated browser profile,
  loopback-only CDP, exact hostname validation, minimized extraction)
- RoboForm-safe browser isolation with no credential handling
- human-challenge (Cloudflare/CAPTCHA/MFA/passkey) alerts with automatic resume
- safe portal form snapshot/fill, manifest-bound upload, and non-final
  continue/preview controls
- a hard block on the irreversible final application submission
- an OpenCode portal-operator sandbox with no raw browser MCP
- privacy and security guards, including a reusable privacy scanner

These extensions were **not authored by the upstream author** and should not be
attributed to them. Any issues with the extensions belong to this fork.

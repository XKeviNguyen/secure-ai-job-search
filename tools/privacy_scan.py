#!/usr/bin/env python3
"""Fail-closed privacy scan for the public framework.

Two layers:

1. **Generic, always-on checks** that ship with the public repo:
   - required placeholder tokens are present in the pristine templates
     (candidate placeholder integrity);
   - secret/key/token/password patterns;
   - forbidden private-document extensions and browser-state filenames.

2. **Optional local fingerprint denylist** (`.privacy-denylist.local`, one
   fingerprint per line, gitignored). It is loaded if present so a maintainer
   can prove zero leakage of private identifiers without committing them. The
   scanner never prints the matched secret; it reports the file and category
   and redacts the value.

Run from the repo root: ``python3 tools/privacy_scan.py``. Exit 0 when clean,
1 when any finding is reported. Stdlib only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = ROOT / ".privacy-denylist.local"

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
SKIP_SUFFIXES = (".pyc", ".pyo")

# (category, compiled pattern). Kept generic; no candidate values here.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("password-assignment", re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{4,}")),
    ("cookie-assignment", re.compile(r"(?i)(set-)?cookie\s*[:=]\s*['\"][^'\"]{8,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]

FORBIDDEN_EXTENSIONS = {
    ".docx": "private-document",
    ".doc": "private-document",
    ".pdf": "private-document",
    ".png": "private-image",
    ".jpg": "private-image",
    ".jpeg": "private-image",
    ".sqlite": "browser/database",
    ".db": "browser/database",
    ".pem": "key-material",
    ".key": "key-material",
    ".p12": "key-material",
    ".pfx": "key-material",
}

FORBIDDEN_NAMES = {
    ".env": "env-file",
    "cookies": "browser-state",
    "cookie": "browser-state",
    "login data": "browser-state",
    "local state": "browser-state",
    "history": "browser-state",
    "web data": "browser-state",
    "chrome-profile": "browser-profile",
}

# (file, required placeholder). The pristine public template must keep these.
PLACEHOLDER_CHECKS = [
    ("CLAUDE.md", "[YOUR_NAME]"),
    ("cv/main_example.tex", "\\name{[First]}{[Last]}"),
    ("cv/main_example.tex", "\\email{[your.email@example.com]}"),
    ("cover_letters/cover_example.tex", "[YOUR NAME]"),
    (".claude/skills/job-application-assistant/01-candidate-profile.md", "[YOUR_EMAIL]"),
    (".claude/skills/job-application-assistant/04-job-evaluation.md", "[YOUR_PRIMARY_SKILLS]"),
    ("config/candidate-preferences.example.yaml", "max_commute_minutes"),
]

# Binary/asset extensions that are legitimately tracked and not candidate data.
ALLOWED_BINARY_SUFFIXES = {".gif", ".ttf", ".otf", ".woff", ".woff2", ".ico"}


def tracked_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        names = [name for name in result.stdout.split("\0") if name]
        if names:
            return [ROOT / name for name in names]
    except (OSError, subprocess.CalledProcessError):
        pass
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def load_denylist() -> list[str]:
    if not DENYLIST.is_file():
        return []
    return [
        line.strip()
        for line in DENYLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def redact(value: str) -> str:
    value = value.strip()
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def scan() -> list[str]:
    findings: list[str] = []

    for relpath, placeholder in PLACEHOLDER_CHECKS:
        path = ROOT / relpath
        if not path.is_file() or placeholder not in path.read_text(encoding="utf-8", errors="replace"):
            findings.append(
                f"placeholder-integrity: {relpath} is missing required placeholder {placeholder!r}"
            )

    denylist = load_denylist()

    for path in tracked_files():
        if path.resolve() == DENYLIST.resolve():
            continue
        relpath = path.relative_to(ROOT).as_posix()
        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_EXTENSIONS and suffix not in ALLOWED_BINARY_SUFFIXES:
            findings.append(f"{FORBIDDEN_EXTENSIONS[suffix]}: {relpath}")
            continue
        lower_name = path.name.lower()
        for forbidden, category in FORBIDDEN_NAMES.items():
            if lower_name == forbidden or lower_name.startswith(forbidden + "."):
                findings.append(f"{category}: {relpath}")
                break
        if suffix in ALLOWED_BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for category, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(f"{category}: {relpath} (redacted: {redact(match.group(0))})")
        for fingerprint in denylist:
            if fingerprint.lower() in text.lower():
                findings.append(f"candidate-fingerprint: {relpath} (redacted: {redact(fingerprint)})")
        if path.name.lower() in ("changed_files.txt", "seen_jobs.json") and "schema" not in text.lower():
            findings.append(f"private-state: {relpath}")
    return findings


def main() -> int:
    findings = scan()
    if findings:
        print(f"privacy_scan: {len(findings)} finding(s)")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    denylist = load_denylist()
    extra = f", denylist fingerprints={len(denylist)}" if denylist else ""
    print(f"privacy_scan: OK (placeholders present, no secret/private patterns{extra})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

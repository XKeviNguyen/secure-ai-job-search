#!/usr/bin/env python3
"""Provider-neutral authenticated job-portal layer.

This is the deterministic guardrail around an authenticated portal session.  It
knows nothing about which agent runtime drives it: any runtime may call these
commands, and the ``portal`` command in ``.claude/commands/portal.md`` documents
the canonical workflow.  OpenCode + the local Chrome DevTools MCP connector is
one implementation, not the architecture.

Security invariants
-------------------
* Only the dedicated browser profile is used (``tools/job_browser.py``).
* A page keeps portal/auth privilege only on an explicitly allowed hostname;
  lookalike domains and external redirects drop to public context.
* Passwords, cookies and tokens are never read, stored, or logged.  The
  RoboForm vault is never opened, queried, or exported.
* CAPTCHA / MFA / passkey / e-mail verification always returns
  ``USER_ACTION_REQUIRED``; nothing is bypassed.
* Authenticated account DOM is never dumped.  Only a whitelisted job schema is
  extracted from a bounded main/job container.
* The final, irreversible submit is never clicked.  Reaching the final
  confirmation returns ``READY_FOR_FINAL_SUBMISSION`` and stops.

Stdlib only for the pure logic.  Live control needs ``websocket-client`` (or
the local Chrome DevTools MCP connector) and the dedicated browser from
``tools/job_browser.py start``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import job_browser
from job_browser import BrowserError, is_loopback_host


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "application-output.json"

PUBLIC = "PUBLIC"
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTHENTICATED = "AUTHENTICATED"
USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
READY_FOR_APPLICATION = "READY_FOR_APPLICATION"
READY_FOR_REVIEW = "READY_FOR_REVIEW"
READY_FOR_FINAL_SUBMISSION = "READY_FOR_FINAL_SUBMISSION"
SUBMISSION_BLOCKED = "SUBMISSION_BLOCKED"
USER_ACTION_REQUIRED_TIMEOUT = "USER_ACTION_REQUIRED_TIMEOUT"

STATES = (
    PUBLIC,
    AUTH_REQUIRED,
    AUTHENTICATED,
    USER_ACTION_REQUIRED,
    READY_FOR_APPLICATION,
    READY_FOR_REVIEW,
    READY_FOR_FINAL_SUBMISSION,
    SUBMISSION_BLOCKED,
    USER_ACTION_REQUIRED_TIMEOUT,
)

NOTIFICATION_TITLE = "Job Search — Action Required"
NOTIFICATION_APP = "Job Search"
ACTION_BODIES = {
    "captcha": "Complete the Cloudflare/CAPTCHA verification in the Job Search browser.",
    "mfa": "Complete MFA/passkey verification in the Job Search browser.",
    "roboform": "Unlock RoboForm in the Job Search browser.",
    "login": "Log in to CareerCross in the Job Search browser.",
}
DEFAULT_ACTION_BODY = "Complete the required action in the Job Search browser."

JOB_FIELD_ORDER = (
    "portal",
    "portal_job_id",
    "job_url",
    "company",
    "role",
    "department",
    "job_description",
    "required_skills",
    "preferred_skills",
    "experience_requirements",
    "language_requirements",
    "workplace",
    "remote_policy",
    "employment_type",
    "salary",
    "benefits",
    "visa_sponsorship",
    "application_deadline",
    "required_documents",
    "application_questions",
    "application_state",
)

MAX_FIELD_CHARS = 8000
MAX_DESCRIPTION_CHARS = 20000

# Challenge detection: visible-text phrases plus real widget selectors.  Never
# scan script/source URLs - every Cloudflare-fronted site contains the word
# "cloudflare" in a script tag and would otherwise look permanently challenged.
CHALLENGE_TEXT_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "attention required",
    "i'm not a robot",
    "are you a robot",
)
CHALLENGE_WIDGET_SELECTORS = (
    "recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "turnstile",
    "cf-chl",
    "challenge-form",
)
CAPTCHA_MARKERS = CHALLENGE_TEXT_MARKERS + CHALLENGE_WIDGET_SELECTORS
MFA_MARKERS = (
    "verification code",
    "one-time code",
    "two-factor",
    "2fa",
    "passkey",
    "authenticator",
    "確認コード",
    "認証コード",
    "ワンタイム",
)
LOGIN_MARKERS = ("login", "log in", "sign in", "ログイン", "パスワード")
LOGGED_OUT_MARKERS = ("login", "sign in", "ログイン", "新規登録")
LOGGED_IN_MARKERS = ("logout", "log out", "sign out", "ログアウト", "my page", "mypage", "マイページ")
APPLICATION_MARKERS = ("apply for this job", "応募する", "application form", "エントリー")
FINAL_SUBMIT_MARKERS = (
    "submit application",
    "confirm and submit",
    "send application",
    "応募を確定",
    "送信する",
    "提出する",
    "apply now",
)

REDACTION_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]{6,}"),
    re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)authorization\s*[:=]\s*\S+"),
    re.compile(r"(?i)(?:cookie|set-cookie)\s*[:=]\s*\S+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)(?:session|csrf|_token)\s*[:=]\s*[A-Za-z0-9._\-]{8,}"),
)


class PortalError(RuntimeError):
    """Raised for unsafe portal configuration or state."""


class ExternalNavigationError(PortalError):
    """Raised when a URL leaves the exact portal allowlist.

    The dedicated browser holds an authenticated RoboForm-backed session, so
    external employer/research pages must use the ordinary public web workflow
    instead of this privileged profile.
    """


# Fields that must never be read, returned, or filled by the portal layer.
FORBIDDEN_FIELD_RE = re.compile(
    r"(password|passwd|pwd|hidden|csrf|xsrf|token|authenticity|session|secret|_method)",
    re.IGNORECASE,
)

# Labels whose values are material facts and must be explicitly confirmed in
# the local answers file before any automated fill (never changed silently).
SENSITIVE_LABEL_RE = re.compile(
    r"(salary|compensation|給与|年収|visa|スポンサー|在留|work authorization|availability|入社|就業可能|start date)",
    re.IGNORECASE,
)

INTERMEDIATE_CONTROL_MARKERS = (
    "next",
    "continue",
    "proceed",
    "preview",
    "review",
    "確認",
    "次へ",
    "進む",
)


@dataclass(frozen=True)
class PortalSpec:
    portal_id: str
    display_name: str
    allowed_hosts: tuple[str, ...]
    home_url: str
    login_url: str
    job_url_markers: tuple[str, ...]
    application_url_markers: tuple[str, ...]


PORTALS: dict[str, PortalSpec] = {
    "careercross": PortalSpec(
        portal_id="careercross",
        display_name="CareerCross",
        allowed_hosts=("careercross.com", "www.careercross.com"),
        home_url="https://www.careercross.com/en/",
        login_url="https://www.careercross.com/en/login",
        job_url_markers=("/job/detail", "/job-detail/", "/job/", "/jobs/"),
        application_url_markers=("/application", "/apply"),
    ),
}


def get_portal(portal_id: str) -> PortalSpec:
    try:
        return PORTALS[portal_id.strip().lower()]
    except KeyError as error:
        supported = ", ".join(sorted(PORTALS))
        raise PortalError(f"unknown portal {portal_id!r}; supported: {supported}") from error


def normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Exact hostname match only.  No wildcard or suffix matching."""
    candidate = normalize_host(host)
    if not candidate:
        return False
    return candidate in {normalize_host(item) for item in allowed_hosts}


def classify_url(portal: PortalSpec, url: str) -> dict:
    """Return portal context and auth privileges for a URL.

    Auth privilege is granted only for exact allowed hosts.  An external
    employer page may still be opened for public research, but it never
    inherits the portal session.
    """
    parts = urlsplit(url)
    host = normalize_host(parts.hostname or "")
    allowed = host_allowed(host, portal.allowed_hosts)
    path = (parts.path or "").lower()
    return {
        "url": url,
        "host": host,
        "allowed": allowed,
        "context": "portal" if allowed else "external",
        "auth_privilege": allowed,
        "is_job": allowed and any(marker in path for marker in portal.job_url_markers),
        "is_application": allowed and any(
            marker in path for marker in portal.application_url_markers
        ),
    }


def require_portal_privilege(portal: PortalSpec, url: str) -> dict:
    classified = classify_url(portal, url)
    if not classified["auth_privilege"]:
        raise PortalError(
            f"refusing portal session on external host {classified['host']!r}; "
            "external pages are public-research targets only"
        )
    return classified


def redact_secrets(text: str) -> str:
    """Replace credential-like values before any log/output."""
    redacted = text
    for pattern in REDACTION_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def _clean(value: object, limit: int = MAX_FIELD_CHARS) -> str:
    text = re.sub(r"[ \t]+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n[truncated]"
    return text


def filter_job_fields(raw: dict) -> dict:
    """Keep the whitelisted job schema only; drop account/portal chrome."""
    job: dict[str, str] = {}
    for key in JOB_FIELD_ORDER:
        if key not in raw:
            continue
        limit = MAX_DESCRIPTION_CHARS if key == "job_description" else MAX_FIELD_CHARS
        job[key] = _clean(raw.get(key), limit)
    return job


def portal_job_id(url: str) -> str:
    match = re.search(r"(\d{4,})", urlsplit(url).path)
    return match.group(1) if match else ""


def classify_state(signals: dict) -> str:
    """Map page signals to a portal state.  Never guesses past a challenge."""
    if signals.get("has_captcha") or signals.get("has_mfa"):
        return USER_ACTION_REQUIRED
    authenticated = bool(signals.get("authenticated") or signals.get("has_logout"))
    if signals.get("has_final_submit"):
        return READY_FOR_FINAL_SUBMISSION
    if signals.get("application_form"):
        return READY_FOR_APPLICATION if authenticated else AUTH_REQUIRED
    if signals.get("review_form"):
        return READY_FOR_REVIEW
    if (signals.get("has_password_input") or signals.get("login_form")) and not authenticated:
        return AUTH_REQUIRED
    if authenticated:
        return AUTHENTICATED
    return PUBLIC


def assert_no_final_submit(action: str) -> None:
    """The final irreversible submit is out of scope for v1."""
    normalized = (action or "").strip().lower()
    if not normalized:
        raise PortalError("refusing an empty browser action")
    if any(marker in normalized for marker in FINAL_SUBMIT_MARKERS):
        raise PortalError(
            "refusing to perform the final application submission; "
            "return READY_FOR_FINAL_SUBMISSION and let the user submit manually"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(manifest_path: Path) -> dict:
    path = manifest_path.expanduser()
    if not path.is_file() or path.name != MANIFEST_NAME:
        raise PortalError(f"not an application manifest: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PortalError(f"unreadable manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PortalError("unsupported manifest schema")
    for key in ("application_id", "application_dir", "documents"):
        if key not in manifest:
            raise PortalError(f"manifest missing {key!r}")
    return manifest


def verify_upload_documents(
    manifest_path: Path,
    *,
    expected_application_id: str | None = None,
    kinds: tuple[str, ...] | None = None,
) -> list[dict]:
    """Resolve upload candidates by exact path + SHA-256 inside one attempt.

    Refuses any path outside the manifest's own application directory, so a
    document from another attempt can never be uploaded by substitution.
    """
    manifest = load_manifest(manifest_path)
    application_id = str(manifest["application_id"])
    if expected_application_id and expected_application_id != application_id:
        raise PortalError(
            f"attempt mismatch: manifest is {application_id!r}, expected {expected_application_id!r}"
        )
    attempt_dir = (REPO_ROOT / str(manifest["application_dir"])).resolve()
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise PortalError("manifest has no routed documents")

    selected: list[dict] = []
    for item in documents:
        if not isinstance(item, dict):
            raise PortalError("malformed document entry in manifest")
        kind = item.get("kind")
        if kinds and kind not in kinds:
            continue
        for key in ("working_path", "pdf_path"):
            value = item.get(key)
            if not value:
                continue
            path = (REPO_ROOT / value).resolve()
            if attempt_dir not in path.parents:
                raise PortalError(
                    f"refusing a document outside attempt {application_id!r}: {value}"
                )
            if not path.is_file():
                raise PortalError(f"document missing: {value}")
            selected.append(
                {
                    "kind": kind,
                    "slot": key,
                    "path": value,
                    "sha256": sha256_file(path),
                }
            )
    if not selected:
        raise PortalError("no matching upload documents found in the manifest")
    return selected


@dataclass
class PortalSession:
    portal: PortalSpec
    port: int = job_browser.DEFAULT_PORT
    host: str = job_browser.DEBUG_HOST
    history: list[dict] = field(default_factory=list)

    def _client(self):
        if not is_loopback_host(self.host):
            raise PortalError("DevTools must be loopback only")
        return DevToolsClient(port=self.port, host=self.host)

    def goto(self, url: str) -> dict:
        """Navigate only within the exact portal allowlist.

        Anything outside the allowlist - a direct external URL or a redirect
        that lands there - stops the authenticated session instead of
        continuing in the privileged browser.
        """
        classified = classify_url(self.portal, url)
        if not classified["auth_privilege"]:
            raise ExternalNavigationError(
                f"refusing external host {classified['host']!r} in the authenticated browser; "
                "use the public web/research workflow instead"
            )
        result = self._client().navigate(url)
        final = classify_url(self.portal, str(result.get("url") or url))
        if not final["auth_privilege"]:
            raise ExternalNavigationError(
                f"navigation left the approved hosts (redirect to {final['host']!r}); stopping"
            )
        self.history.append(
            {"action": "goto", "host": final["host"], "privilege": True}
        )
        return result

    def snapshot(self) -> dict:
        return self._client().snapshot()

    def state(self) -> str:
        return classify_state(self.snapshot()["signals"])

    def extract_job(self, url: str) -> dict:
        classified = require_portal_privilege(self.portal, url)
        if not classified["is_job"]:
            raise PortalError(f"not a recognized {self.portal.display_name} job URL: {url}")
        self.goto(url)
        snapshot = self.snapshot()
        state = classify_state(snapshot["signals"])
        if state in (AUTH_REQUIRED, USER_ACTION_REQUIRED):
            return {"state": state, "job": {}}
        raw = snapshot.get("job", {})
        raw.setdefault("portal", self.portal.portal_id)
        raw.setdefault("portal_job_id", portal_job_id(url))
        raw.setdefault("job_url", url)
        job = filter_job_fields(raw)
        job["application_state"] = state
        return {"state": state, "job": job}

    # ---- safe login -----------------------------------------------------

    def login(self, *, wait_seconds: float = 15.0) -> dict:
        if self.portal.portal_id != "careercross":
            raise PortalError("login is implemented for careercross only")
        self.goto(self.portal.login_url)
        deadline = time.time() + wait_seconds
        while True:
            snapshot = self.snapshot()
            state = classify_state(snapshot["signals"])
            if state == USER_ACTION_REQUIRED:
                return {"state": USER_ACTION_REQUIRED, "reason": "challenge present"}
            form = self._client().evaluate(LOGIN_FORM_JS)
            if not isinstance(form, dict) or not form.get("form"):
                if snapshot["signals"].get("authenticated"):
                    return {"state": AUTHENTICATED, "reason": "already authenticated"}
                return {"state": USER_ACTION_REQUIRED, "reason": "no login form found"}
            if not host_allowed(str(form.get("action_host") or ""), self.portal.allowed_hosts):
                raise PortalError("login form action is outside the approved hosts")
            if form.get("username_present") and form.get("password_present"):
                if not form.get("has_submit"):
                    return {"state": USER_ACTION_REQUIRED, "reason": "no login control found"}
                self._client().evaluate(LOGIN_SUBMIT_JS)
                return self._await_authenticated(wait_seconds)
            if time.time() >= deadline:
                return {
                    "state": USER_ACTION_REQUIRED,
                    "reason": "RoboForm login fields are empty; unlock/use RoboForm in the dedicated browser",
                }
            time.sleep(0.5)

    def _await_authenticated(self, wait_seconds: float) -> dict:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            snapshot = self.snapshot()
            state = classify_state(snapshot["signals"])
            if state == USER_ACTION_REQUIRED:
                return {"state": USER_ACTION_REQUIRED, "reason": "challenge after login"}
            if state in (AUTHENTICATED, READY_FOR_APPLICATION, READY_FOR_REVIEW):
                return {"state": AUTHENTICATED, "reason": "login confirmed"}
            time.sleep(0.25)
        return {"state": USER_ACTION_REQUIRED, "reason": "could not confirm login"}

    # ---- form snapshot / fill ------------------------------------------

    def form_snapshot(self) -> dict:
        state = self.state()
        fields = self._client().evaluate(FORM_SNAPSHOT_JS)
        if not isinstance(fields, list):
            raise PortalError("form snapshot failed")
        safe_fields = [self._sanitize_field(field) for field in fields]
        return {"state": state, "fields": safe_fields}

    @staticmethod
    def _sanitize_field(field: dict) -> dict:
        name = str(field.get("name") or "")
        identifier = str(field.get("identifier") or "")
        if FORBIDDEN_FIELD_RE.search(name) or FORBIDDEN_FIELD_RE.search(identifier):
            raise PortalError("form snapshot attempted to expose a forbidden field")
        return {
            "identifier": identifier,
            "label": _clean(field.get("label"), 200),
            "type": _clean(field.get("type"), 40),
            "required": bool(field.get("required")),
            "maxlength": field.get("maxlength"),
            "options": [
                {"value": _clean(option.get("value"), 200), "label": _clean(option.get("label"), 200)}
                for option in (field.get("options") or [])
                if isinstance(option, dict)
            ],
            "value_state": "non-empty" if field.get("value_state") == "non-empty" else "empty",
        }

    def fill(self, answers: dict) -> dict:
        if not isinstance(answers, dict) or not isinstance(answers.get("fields"), dict):
            raise PortalError("answers file must contain a 'fields' object")
        fields = {field["identifier"]: field for field in self.form_snapshot()["fields"]}
        confirmed_sensitive = bool(answers.get("confirmed_sensitive"))
        validated: dict[str, str] = {}
        unresolved: list[str] = []

        for identifier, value in answers["fields"].items():
            if identifier not in fields:
                raise PortalError(f"unknown field identifier in answers: {identifier!r}")
            if FORBIDDEN_FIELD_RE.search(identifier):
                raise PortalError(f"refusing to fill forbidden field {identifier!r}")
            field = fields[identifier]
            text = "" if value is None else str(value)
            if field["type"] in ("hidden", "password", "submit", "button", "file"):
                raise PortalError(f"refusing to fill {field['type']} field {identifier!r}")
            if SENSITIVE_LABEL_RE.search(field.get("label") or "") and not confirmed_sensitive:
                return {
                    "state": USER_ACTION_REQUIRED,
                    "reason": "a material fact field requires confirmed_sensitive: true",
                    "field": identifier,
                }
            maxlength = field.get("maxlength")
            if maxlength and str(maxlength).isdigit() and len(text) > int(maxlength):
                raise PortalError(f"value for {identifier!r} exceeds maxlength {maxlength}")
            options = field.get("options") or []
            if field["type"] == "select" and options:
                allowed = {option["value"] for option in options}
                if text not in allowed:
                    raise PortalError(f"value for {identifier!r} is not an allowed option")
            if field["type"] in ("radio", "checkbox") and options:
                allowed = {option["value"] for option in options}
                if text not in allowed:
                    raise PortalError(f"value for {identifier!r} is not an allowed option")
            validated[identifier] = text

        results = self._client().evaluate(_fill_expression(validated))
        if not isinstance(results, dict) or results.get("refused"):
            raise PortalError(f"browser refused to fill: {results}")

        for identifier, field in fields.items():
            if not field.get("required"):
                continue
            if identifier not in validated and field.get("value_state") != "non-empty":
                unresolved.append(identifier)
        if unresolved:
            return {
                "state": USER_ACTION_REQUIRED,
                "reason": "unresolved required fields; provide values in the answers file",
                "missing_required": unresolved,
                "filled": sorted(validated),
            }
        return {"state": AUTHENTICATED, "filled": sorted(validated), "missing_required": []}

    # ---- manifest-bound upload -----------------------------------------

    def upload(self, documents: list[dict]) -> dict:
        if not documents:
            raise PortalError("no documents to upload")
        for document in documents:
            if not document.get("path") or not document.get("sha256"):
                raise PortalError("upload documents must carry path and sha256")
        plan = self._client().evaluate(UPLOAD_PLAN_JS)
        if not isinstance(plan, dict) or not plan.get("file_inputs"):
            return {"state": USER_ACTION_REQUIRED, "reason": "no file input on this page"}
        inputs = int(plan["file_inputs"])
        if inputs != len(documents):
            return {
                "state": USER_ACTION_REQUIRED,
                "reason": "file-input count does not match the manifest document count",
                "file_inputs": inputs,
                "documents": len(documents),
            }
        self._client().call("DOM.enable")
        root = self._client().call("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
        assigned = []
        for index, document in enumerate(documents):
            node = self._client().call(
                "DOM.querySelector",
                {"nodeId": root, "selector": f'input[data-portal-upload="{index}"]'},
            )
            node_id = node.get("nodeId")
            if not node_id:
                raise PortalError("file input disappeared before upload")
            absolute = str((REPO_ROOT / document["path"]).resolve())
            self._client().call(
                "DOM.setFileInputFiles",
                {"files": [absolute], "nodeId": node_id},
            )
            assigned.append(
                {"kind": document.get("kind"), "path": document["path"], "sha256": document["sha256"]}
            )
        return {"state": READY_FOR_REVIEW, "uploaded": assigned}

    # ---- non-final continue / preview ----------------------------------

    def continue_step(self) -> dict:
        snapshot = self.snapshot()
        state = classify_state(snapshot["signals"])
        if state == READY_FOR_FINAL_SUBMISSION:
            return {"state": READY_FOR_FINAL_SUBMISSION, "clicked": False}
        controls = self._client().evaluate(CONTROL_SCAN_JS)
        if not isinstance(controls, list):
            raise PortalError("control scan failed")
        candidates = [
            control
            for control in controls
            if control.get("intermediate") and not control.get("final")
        ]
        if not candidates:
            return {"state": state, "clicked": False, "reason": "no intermediate control"}
        if len(candidates) > 1:
            return {
                "state": SUBMISSION_BLOCKED,
                "clicked": False,
                "reason": "ambiguous controls; not clicking",
            }
        control = candidates[0]
        label = str(control.get("text") or "")
        assert_no_final_submit(label)
        self._client().evaluate(_click_control_expression(int(control["index"])))
        return {"state": state, "clicked": True, "control": _clean(label, 80)}


JOB_PROBE_JS = r"""
(() => {
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '');
  const clean = (s) => (s || '').replace(/[ \t\u3000]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const isVisible = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;

  const containerSelectors = [
    '#job_design',
    'main',
    'article',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '#contents',
  ];
  let container = null;
  for (const selector of containerSelectors) {
    container = document.querySelector(selector);
    if (container) break;
  }

  let jobText = '';
  if (container) {
    const clone = container.cloneNode(true);
    clone
      .querySelectorAll(
        'nav,header,footer,aside,script,style,noscript,' +
        '[class*="nav"],[class*="menu"],[class*="sidebar"],[class*="account"],' +
        '[class*="mypage"],[class*="my-page"],[class*="modal"],[class*="banner"],' +
        '[class*="breadcrumb"],[class*="search-form"],[class*="recommend"],' +
        '[class*="related"]'
      )
      .forEach((el) => el.remove());
    jobText = clean(text(clone));
  }

  const heading = container ? clean(text(container.querySelector('h1'))) : '';
  const role = heading.includes('|') ? clean(heading.split('|').pop()) : '';

  const sections = {};
  if (container) {
    for (const node of Array.from(container.querySelectorAll('h2,h3'))) {
      const name = clean(text(node)).toLowerCase();
      if (!name) continue;
      let body = '';
      let cursor = node.nextElementSibling;
      while (cursor && !/^H[23]$/.test(cursor.tagName)) {
        body += '\n' + clean(text(cursor));
        cursor = cursor.nextElementSibling;
      }
      sections[name] = clean(body);
    }
  }
  const section = (needles) => {
    for (const name of Object.keys(sections)) {
      if (needles.some((needle) => name.includes(needle))) return sections[name];
    }
    return '';
  };
  const headerLine = (label) => {
    const pattern = new RegExp(label + '\\s*[:：]\\s*([^\\n]{0,300})', 'i');
    const match = jobText.match(pattern);
    return match ? clean(match[1]) : '';
  };
  const shortElement = (selectors) => {
    for (const selector of selectors) {
      for (const el of Array.from(document.querySelectorAll(selector))) {
        if (!isVisible(el)) continue;
        const value = clean(text(el));
        if (value && value.length <= 120) return value;
      }
    }
    return '';
  };

  const bodyVisible = clean(document.body ? document.body.innerText : '').toLowerCase();
  const hasText = (markers) => markers.some((marker) => bodyVisible.includes(marker));
  const visiblePassword = Array.from(document.querySelectorAll('input[type="password"]')).some(isVisible);

  return {
    url: location.href,
    title: (document.title || '').slice(0, 300),
    main_text: jobText.slice(0, 20000),
    inputs: [],
    signals: {
      has_password_input: visiblePassword,
      login_form: visiblePassword,
      has_captcha:
        !!document.querySelector(
          'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .cf-turnstile, ' +
            '[id*="turnstile"], form#challenge-form, [id*="cf-chl"]'
        ) ||
        hasText(["i'm not a robot", 'are you a robot', 'just a moment', 'checking your browser', 'verify you are human', 'attention required']),
      has_mfa: hasText(['verification code', 'one-time code', 'two-factor', '2fa', 'passkey', 'authenticator', '確認コード', '認証コード', 'ワンタイム']),
      has_logout: hasText(['logout', 'log out', 'sign out', 'ログアウト', 'マイページ', 'my page', 'mypage']),
      login_form_present: visiblePassword,
      application_form: hasText(['application form', 'apply for this job', 'apply now', 'send application', '応募する', 'エントリー']),
      review_form:
        hasText(['確認画面', '内容確認', 'preview page']) ||
        !!document.querySelector('[class*="confirm"] input[type="submit"], button[class*="confirm"]'),
      has_final_submit:
        hasText(['submit application', 'confirm and submit', '応募を確定', '応募を送信', 'application complete', 'application submitted']) ||
        Array.from(document.querySelectorAll('button,input[type="submit"],a')).some((el) => {
          if (!isVisible(el)) return false;
          const label = clean(el.innerText || el.value || '');
          return /(応募|application|apply)/i.test(label) && /(確定|送信|submit|confirm|complete)/i.test(label);
        }),
    },
    job: {
      company: shortElement([
        '#company_name',
        '.companyName',
        '.company-name',
        '[class*="companyName"]',
        '[class*="company-name"]',
      ]),
      role: role,
      department: section(['job category']) || headerLine('Department'),
      job_description: section(['job description', '仕事内容', '職務内容']) || headerLine('Job Description'),
      required_skills: section(['general requirements', 'required skill', '応募資格', '必須']) || headerLine('Requirements'),
      preferred_skills: section(['preferred skill', '歓迎']) || headerLine('Preferred'),
      experience_requirements: section(['experience']) || headerLine('Experience'),
      language_requirements: section(['language', '語学']) || headerLine('Language'),
      workplace: section(['job location', '勤務地']) || headerLine('Location'),
      remote_policy: headerLine('Remote'),
      employment_type: headerLine('Job Type') || headerLine('Employment Type'),
      salary: headerLine('Salary') || headerLine('給与'),
      benefits: section(['work conditions', '待遇', '福利厚生']) || headerLine('Benefits'),
      visa_sponsorship: headerLine('Visa'),
      application_deadline: headerLine('Application Deadline') || headerLine('応募締切'),
      required_documents: headerLine('Documents'),
      application_questions: '',
    },
  };
})()
"""


LOGIN_FORM_JS = r"""
(() => {
  const isVisible = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
  const forms = Array.from(document.querySelectorAll('form')).filter((form) => {
    const password = form.querySelector('input[type="password"]');
    return password && isVisible(password);
  });
  if (!forms.length) return { form: false };
  const form = forms[0];
  const password = form.querySelector('input[type="password"]');
  const username = form.querySelector(
    'input[type="text"],input[type="email"],input[name*="user" i],input[name*="email" i],input[name*="login" i]'
  );
  let host = '';
  try {
    host = new URL(form.getAttribute('action') || location.href, location.href).hostname.toLowerCase();
  } catch (error) {
    host = '';
  }
  return {
    form: true,
    action_host: host,
    username_present: !!(username && (username.value || '').length > 0),
    password_present: !!(password && (password.value || '').length > 0),
    has_submit: !!form.querySelector('button[type="submit"],input[type="submit"],button:not([type])'),
  };
})()
"""

LOGIN_SUBMIT_JS = r"""
(() => {
  const isVisible = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
  const forms = Array.from(document.querySelectorAll('form')).filter((form) => {
    const password = form.querySelector('input[type="password"]');
    return password && isVisible(password);
  });
  if (!forms.length) return false;
  const control = forms[0].querySelector('button[type="submit"],input[type="submit"],button:not([type])');
  if (!control || !isVisible(control)) return false;
  control.click();
  return true;
})()
"""

FORM_SNAPSHOT_JS = r"""
(() => {
  const isVisible = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
  const labelFor = (el) => {
    if (el.id) {
      const linked = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (linked) return (linked.innerText || '').trim().slice(0, 200);
    }
    const wrapped = el.closest('label');
    if (wrapped) return (wrapped.innerText || '').trim().slice(0, 200);
    const previous = el.previousElementSibling;
    if (previous && (previous.innerText || '').trim()) return (previous.innerText || '').trim().slice(0, 200);
    return el.getAttribute('aria-label') || el.placeholder || '';
  };
  const out = [];
  for (const el of document.querySelectorAll('input,textarea,select')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || tag).toLowerCase();
    if (['password', 'hidden', 'submit', 'button', 'image', 'file'].includes(type)) continue;
    if (!isVisible(el)) continue;
    const name = el.getAttribute('name') || '';
    const identifier = el.id || name;
    if (!identifier) continue;
    const options = [];
    if (tag === 'select') {
      for (const option of Array.from(el.options).slice(0, 80)) {
        options.push({ value: (option.value || '').slice(0, 200), label: (option.text || '').trim().slice(0, 120) });
      }
    } else if (type === 'radio' || type === 'checkbox') {
      options.push({ value: (el.value || '').slice(0, 200), label: labelFor(el) });
    }
    out.push({
      identifier: identifier,
      name: name,
      label: labelFor(el),
      type: type,
      required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
      maxlength: el.getAttribute('maxlength') || null,
      options: options,
      value_state: (el.value || '').length > 0 ? 'non-empty' : 'empty',
    });
  }
  return out;
})()
"""

UPLOAD_PLAN_JS = r"""
(() => {
  const isVisible = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
  const inputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(isVisible);
  inputs.forEach((el, index) => el.setAttribute('data-portal-upload', String(index)));
  return { file_inputs: inputs.length };
})()
"""

CONTROL_SCAN_JS = r"""
(() => {
  const isVisible = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const finalRe = /(submit application|confirm and submit|send application|応募を確定|応募を送信|application complete)/i;
  const applyRe = /(応募|apply|application)/i;
  const confirmRe = /(確定|送信|submit|confirm|complete)/i;
  const intermediateRe = /(next|continue|proceed|preview|review|確認|次へ|進む)/i;
  const nodes = Array.from(
    document.querySelectorAll('button,input[type="submit"],input[type="button"],a[role="button"],a.button')
  );
  const out = [];
  let index = 0;
  for (const el of nodes) {
    if (!isVisible(el)) continue;
    const text = clean(el.innerText || el.value || el.getAttribute('aria-label') || '');
    if (!text) continue;
    const final = finalRe.test(text) || (applyRe.test(text) && confirmRe.test(text));
    const intermediate = intermediateRe.test(text) && !final && !applyRe.test(text);
    el.setAttribute('data-portal-control', String(index));
    out.push({ index: index, text: text.slice(0, 80), role: el.tagName.toLowerCase(), final: final, intermediate: intermediate });
    index += 1;
  }
  return out;
})()
"""


def _fill_expression(answers: dict) -> str:
    payload = json.dumps(answers, ensure_ascii=False)
    return r"""
(() => {
  const answers = %s;
  const results = { filled: [], refused: [] };
  for (const identifier of Object.keys(answers)) {
    const el = document.getElementById(identifier) || document.querySelector('[name="' + CSS.escape(identifier) + '"]');
    if (!el) { results.refused.push(identifier); continue; }
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || tag).toLowerCase();
    const name = el.getAttribute('name') || '';
    if (['password', 'hidden', 'file', 'submit', 'button'].includes(type)) { results.refused.push(identifier); continue; }
    if (/(password|passwd|pwd|hidden|csrf|xsrf|token|authenticity|session|secret)/i.test(name + ' ' + identifier)) {
      results.refused.push(identifier);
      continue;
    }
    el.value = answers[identifier];
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    results.filled.push(identifier);
  }
  return results;
})()
""" % payload


def _click_control_expression(index: int) -> str:
    return (
        "(() => { const el = document.querySelector('[data-portal-control=\""
        + str(int(index))
        + "\"]'); if (!el) return false; el.click(); return true; })()"
    )


class DevToolsClient:
    """Minimal loopback-only Chrome DevTools Protocol client."""

    def __init__(self, port: int = job_browser.DEFAULT_PORT, host: str = job_browser.DEBUG_HOST, timeout: float = 20.0) -> None:
        if not is_loopback_host(host):
            raise PortalError(f"refusing a non-loopback DevTools host: {host}")
        if not 1 <= int(port) <= 65535:
            raise PortalError(f"invalid DevTools port: {port}")
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._ws = None
        self._next_id = 0

    def _base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def targets(self) -> list[dict]:
        payload = job_browser.http_json(f"{self._base()}/json", timeout=3.0)
        if not isinstance(payload, list):
            raise PortalError(
                f"no DevTools endpoint on {self.host}:{self.port}; run `python tools/job_browser.py start`"
            )
        return payload

    def page_target(self) -> dict:
        pages = [t for t in self.targets() if t.get("type") == "page"]
        if not pages:
            raise PortalError("no page target available in the dedicated browser")
        for page in pages:
            if page.get("url", "").startswith("http"):
                return page
        return pages[0]

    def connect(self) -> None:
        if self._ws is not None:
            return
        try:
            import websocket
        except ImportError as error:
            raise PortalError(
                "websocket-client is required for live browser control; install the pinned "
                "version with `python3 -m pip install -r tools/requirements.txt`"
            ) from error
        target = self.page_target()
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise PortalError("page target has no webSocketDebuggerUrl")
        self._ws = websocket.create_connection(
            ws_url,
            timeout=self.timeout,
            max_size=50 * 1024 * 1024,
            suppress_origin=True,
        )

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None

    def call(self, method: str, params: dict | None = None) -> dict:
        self.connect()
        self._next_id += 1
        message_id = self._next_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = self._ws.recv()
            if not raw:
                raise PortalError(f"DevTools connection closed during {method}")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != message_id:
                continue
            if "error" in payload:
                raise PortalError(f"{method} failed: {redact_secrets(str(payload['error']))}")
            return payload.get("result", {})

    def evaluate(self, expression: str, *, await_promise: bool = False) -> object:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": False,
            },
        )
        if result.get("exceptionDetails"):
            raise PortalError("page script error during extraction")
        return result.get("result", {}).get("value")

    def navigate(self, url: str) -> dict:
        import time

        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        for _ in range(80):
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except PortalError:
                pass
            time.sleep(0.25)
        try:
            return {
                "url": self.evaluate("location.href"),
                "title": self.evaluate("document.title"),
            }
        except PortalError:
            return {"url": url, "title": ""}

    def snapshot(self) -> dict:
        value = self.evaluate(JOB_PROBE_JS)
        if not isinstance(value, dict):
            raise PortalError("page probe returned no data")
        value.setdefault("signals", {})
        value["signals"]["authenticated"] = bool(value["signals"].get("has_logout"))
        return value


def classify_blocker(signals: dict) -> str | None:
    """Map non-sensitive page signals to a human-action blocker, or None."""
    if signals.get("has_captcha"):
        return "captcha"
    if signals.get("has_mfa"):
        return "mfa"
    if signals.get("has_password_input") or signals.get("login_form"):
        return "roboform"
    return None


def action_body_for(blocker: str) -> str:
    return ACTION_BODIES.get(blocker, DEFAULT_ACTION_BODY)


def send_desktop_notification(body: str) -> str:
    """Send a sanitized local desktop notification; fall back to the terminal.

    Only static, sanitized content is sent - never credentials, page content,
    or application answers. A missing or failing notify-send degrades to a
    terminal bell plus a clear message and never fails the workflow.
    """
    if shutil.which("notify-send"):
        try:
            result = subprocess.run(
                ["notify-send", f"--app-name={NOTIFICATION_APP}", NOTIFICATION_TITLE, body],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if result.returncode == 0:
                return "notify-send"
        except (OSError, subprocess.SubprocessError):
            pass
    sys.stdout.write("\a")
    sys.stdout.flush()
    print(f"USER_ACTION_REQUIRED: {body}")
    return "terminal"


def _login_fields_populated(session: "PortalSession") -> bool:
    """Check only boolean field presence after RoboForm autofill."""
    try:
        form = session._client().evaluate(LOGIN_FORM_JS)
    except PortalError:
        return False
    if not isinstance(form, dict) or not form.get("form"):
        return False
    if not host_allowed(str(form.get("action_host") or ""), session.portal.allowed_hosts):
        return False
    return bool(form.get("username_present") and form.get("password_present"))


def wait_for_user_action(
    session: "PortalSession",
    *,
    timeout: float = 300.0,
    interval: float = 3.0,
    notify: bool = True,
) -> dict:
    """Notify the user, then poll until the blocking challenge clears.

    Polls only non-sensitive signals. It never clicks or solves a challenge and
    never reads a field value. For a RoboForm blocker it runs the existing safe
    login flow once the fields become populated. A cleared challenge resumes
    automatically; a timeout returns ``USER_ACTION_REQUIRED_TIMEOUT`` and
    leaves the browser open.
    """
    interval = max(0.5, float(interval))
    snapshot = session.snapshot()
    signals = snapshot["signals"]
    blocker = classify_blocker(signals)
    if blocker is None:
        return {"state": classify_state(signals), "resumed": True, "blocker": None}

    try:
        session._client().call("Page.bringToFront")
    except (PortalError, AttributeError, OSError):
        pass
    if notify:
        send_desktop_notification(action_body_for(blocker))
    else:
        print(f"USER_ACTION_REQUIRED: {action_body_for(blocker)}")

    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            current_snapshot = session.snapshot()
        except PortalError:
            continue
        current_signals = current_snapshot["signals"]
        if classify_blocker(current_signals) is None:
            return {
                "state": classify_state(current_signals),
                "resumed": True,
                "blocker": blocker,
            }
        if blocker == "roboform" and _login_fields_populated(session):
            outcome = session.login()
            resumed = outcome.get("state") != USER_ACTION_REQUIRED
            return {
                "state": outcome.get("state"),
                "resumed": resumed,
                "blocker": "roboform",
            }
    return {
        "state": USER_ACTION_REQUIRED_TIMEOUT,
        "resumed": False,
        "blocker": blocker,
        "timeout": timeout,
    }


def _portal_url_parser(sub, name: str, help_text: str, *, url_required: bool = True):
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--portal", default="careercross")
    if url_required:
        parser.add_argument("--url", required=True)
    parser.add_argument("--port", type=int, default=job_browser.DEFAULT_PORT)
    parser.add_argument("--json", action="store_true")
    return parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check browser, raw-MCP absence, operator and guardrails")

    status = sub.add_parser("status", help="show dedicated browser status")
    status.add_argument("--port", type=int, default=job_browser.DEFAULT_PORT)

    _portal_url_parser(sub, "read", "read one job posting from an authenticated portal")
    _portal_url_parser(sub, "open", "open a portal page and report only its state")
    _portal_url_parser(sub, "form-snapshot", "return safe application-form metadata only")

    login = _portal_url_parser(sub, "login", "safe CareerCross login via RoboForm autofill", url_required=False)
    login.set_defaults(url=None)

    _portal_url_parser(sub, "continue", "advance one clearly non-final control", url_required=False)

    wait = sub.add_parser(
        "wait-user-action",
        help="notify the user and auto-resume once a human challenge clears",
    )
    wait.add_argument("--portal", default="careercross")
    wait.add_argument("--port", type=int, default=job_browser.DEFAULT_PORT)
    wait.add_argument("--timeout", type=float, default=300.0)
    wait.add_argument("--interval", type=float, default=3.0)
    wait.add_argument("--no-notify", action="store_true")
    wait.add_argument("--json", action="store_true")

    fill = _portal_url_parser(sub, "fill", "fill safe fields from a local answers file")
    fill.add_argument("--answers", type=Path, required=True)

    upload = _portal_url_parser(sub, "upload", "upload manifest-resolved documents")
    upload.add_argument("--manifest", type=Path, required=True)
    upload.add_argument("--kind", action="append", default=[])

    docs = sub.add_parser("documents", help="verify upload documents for one attempt")
    docs.add_argument("--manifest", type=Path, required=True)
    docs.add_argument("--application-id", default=None)
    docs.add_argument("--kind", action="append", default=[])
    docs.add_argument("--json", action="store_true")
    return parser


def _load_answers(path: Path) -> dict:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PortalError(f"unreadable answers file: {error}") from error
    if not isinstance(data, dict):
        raise PortalError("answers file must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = job_browser.doctor(job_browser.DEFAULT_PROFILE, job_browser.DEFAULT_PORT)
        elif args.command == "status":
            payload = job_browser.status(job_browser.DEFAULT_PROFILE, args.port)
        elif args.command == "documents":
            kinds = tuple(args.kind) or None
            payload = {
                "documents": verify_upload_documents(
                    args.manifest, expected_application_id=args.application_id, kinds=kinds
                )
            }
        else:
            portal = get_portal(args.portal)
            session = PortalSession(portal=portal, port=args.port)
            if args.command == "open":
                session.goto(args.url)
                snapshot = session.snapshot()
                payload = {
                    "state": classify_state(snapshot["signals"]),
                    "portal": portal.portal_id,
                    "host": classify_url(portal, str(snapshot.get("url", args.url)))["host"],
                    "title": _clean(snapshot.get("title"), 200),
                    "url": _clean(snapshot.get("url"), 300),
                }
            elif args.command == "read":
                payload = session.extract_job(args.url)
            elif args.command == "login":
                payload = session.login()
            elif args.command == "form-snapshot":
                session.goto(args.url)
                payload = session.form_snapshot()
            elif args.command == "fill":
                session.goto(args.url)
                payload = session.fill(_load_answers(args.answers))
            elif args.command == "upload":
                session.goto(args.url)
                documents = verify_upload_documents(
                    args.manifest, kinds=tuple(args.kind) or None
                )
                payload = session.upload(documents)
            elif args.command == "wait-user-action":
                payload = wait_for_user_action(
                    session,
                    timeout=args.timeout,
                    interval=args.interval,
                    notify=not args.no_notify,
                )
            else:
                payload = session.continue_step()
    except (PortalError, BrowserError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

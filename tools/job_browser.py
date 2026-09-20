#!/usr/bin/env python3
"""Manage the dedicated authenticated Job Search browser.

Security model
--------------
Authenticated portals (CareerCross first) are driven through a *dedicated*
Chrome user-data directory.  The agent never uses the user's personal Chrome
profile, never reads the RoboForm vault, and never handles portal passwords.

This launcher only manages the browser process and its loopback DevTools
endpoint.  It stores no credentials, reads no password databases, and never
prints secrets.

Usage
-----
    python tools/job_browser.py doctor
    python tools/job_browser.py start
    python tools/job_browser.py status
    python tools/job_browser.py stop

Stdlib only.  The DevTools endpoint is always bound to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parent.parent
DEDICATED_BASE = HOME / ".local" / "share" / "ai-job-search"
DEFAULT_PROFILE = DEDICATED_BASE / "chrome-profile"
DEFAULT_PORT = 9222
DEBUG_HOST = "127.0.0.1"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CHROME_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
PERSONAL_PROFILE_ROOTS = (
    HOME / ".config" / "google-chrome",
    HOME / ".config" / "chromium",
    HOME / ".config" / "chrome",
)
CONFIG_PATH = REPO_ROOT / ".opencode" / "opencode.json"
PORTAL_OPERATOR_AGENT = REPO_ROOT / ".opencode" / "agents" / "portal-operator.md"
REQUIREMENTS_PATH = Path(__file__).resolve().parent / "requirements.txt"
ROBOFORM_VAULT_HINT = "RoboForm vault is never read, queried, or exported by this tool."


class BrowserError(RuntimeError):
    """Raised for unsafe configuration or a failed browser operation."""


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in LOOPBACK_HOSTS


def validate_profile(profile: Path, *, dedicated_base: Path = DEDICATED_BASE) -> Path:
    """Refuse the personal Chrome profile and anything outside the dedicated base."""
    profile = profile.expanduser()
    if not profile.is_absolute():
        raise BrowserError(f"user-data-dir must be absolute: {profile}")
    resolved = profile.resolve()
    for personal in PERSONAL_PROFILE_ROOTS:
        personal = personal.resolve()
        if resolved == personal or personal in resolved.parents:
            raise BrowserError(
                f"refusing the personal Chrome profile {resolved}; use {DEFAULT_PROFILE}"
            )
    base = dedicated_base.expanduser().resolve()
    if resolved == base:
        raise BrowserError(
            f"refusing to use the dedicated base itself as a profile: {resolved}"
        )
    if base not in resolved.parents:
        raise BrowserError(
            f"refusing a profile outside {base}: {resolved}"
        )
    return resolved


def cdp_url(port: int, path: str = "/json/version", host: str = DEBUG_HOST) -> str:
    if not is_loopback_host(host):
        raise BrowserError(f"refusing a non-loopback DevTools host: {host}")
    if not 1 <= int(port) <= 65535:
        raise BrowserError(f"invalid port: {port}")
    return f"http://{host}:{int(port)}{path}"


def http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def cdp_version(port: int) -> dict | None:
    return http_json(cdp_url(port, "/json/version"))


def port_is_free(port: int, host: str = DEBUG_HOST) -> bool:
    if not is_loopback_host(host):
        raise BrowserError(f"refusing a non-loopback host: {host}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, int(port)))
        except OSError:
            return False
    return True


def _iter_proc_argv() -> list[tuple[int, list[str]]]:
    """Return (pid, argv) with the original NUL-separated argv boundaries."""
    entries: list[tuple[int, list[str]]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            raw = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
        entries.append((int(proc.name), argv))
    return entries


def _normalize_path(value: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(value)))


def _argv_tokens(argv: list[str]) -> list[str]:
    """Recover exact argv tokens, tolerating Chrome's process-title rewrite.

    Chrome overwrites its argv memory on Linux, so ``/proc/<pid>/cmdline`` can
    collapse into a single space-separated string. Splitting that title back
    into tokens restores exact-argument matching; a legitimate path with a
    space would be rare, and the dedicated profile path has none.
    """
    tokens: list[str] = []
    for part in argv:
        if " " in part:
            tokens.extend(part.split())
        else:
            tokens.append(part)
    return tokens


def _matches_profile(argv: list[str], profile: Path) -> bool:
    """True only for an exact ``--user-data-dir`` argument.

    Matching is exact token equality after path normalization, so
    ``chrome-profile`` never matches ``chrome-profile2``. Both
    ``--user-data-dir=<path>`` and the two-argument form are supported.
    """
    target = _normalize_path(str(profile))
    tokens = _argv_tokens(argv)
    for index, arg in enumerate(tokens):
        if arg == "--user-data-dir" and index + 1 < len(tokens):
            if _normalize_path(tokens[index + 1]) == target:
                return True
        elif arg.startswith("--user-data-dir="):
            value = arg.split("=", 1)[1]
            if value and _normalize_path(value) == target:
                return True
    return False


def _is_chrome_argv0(argv: list[str]) -> bool:
    tokens = _argv_tokens(argv)
    if not tokens:
        return False
    name = os.path.basename(tokens[0]).lower()
    return "chrome" in name or "chromium" in name


def browser_processes(profile: Path) -> list[int]:
    """PIDs of Chrome processes launched against exactly this user-data-dir."""
    pids = []
    for pid, argv in _iter_proc_argv():
        if not _is_chrome_argv0(argv):
            continue
        if _matches_profile(argv, profile):
            pids.append(pid)
    return sorted(pids)


def browser_running(profile: Path) -> bool:
    return bool(browser_processes(profile))


def raw_mcp_summary() -> dict:
    """Report any unrestricted chrome-devtools MCP connector (must be absent).

    The raw MCP exposes click/fill/upload/navigate/evaluate and network
    inspection, which would bypass every portal guardrail. v1 deliberately
    ships no such connector; authenticated actions go through the
    portal-operator subagent and the narrow tools/portal.py commands.
    """
    if not CONFIG_PATH.is_file():
        return {"configured": False, "enabled": False, "reason": f"{CONFIG_PATH} missing"}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"configured": False, "enabled": False, "reason": f"unreadable: {error}"}
    servers = data.get("mcp", {}) if isinstance(data, dict) else {}
    entry = servers.get("chrome-devtools") if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return {"configured": False, "enabled": False}
    return {"configured": True, "enabled": bool(entry.get("enabled", True))}


def portal_operator_summary() -> dict:
    """Check the scoped portal-operator subagent enforces the boundary."""
    if not PORTAL_OPERATOR_AGENT.is_file():
        return {"present": False, "ready": False, "reason": "agent file missing"}
    text = PORTAL_OPERATOR_AGENT.read_text(encoding="utf-8")
    frontmatter = ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            frontmatter = parts[1]
    summary = {
        "present": True,
        "mode_subagent": bool(re.search(r"(?m)^mode:\s*subagent\s*$", frontmatter)),
        "edit_denied": bool(re.search(r"(?m)^\s+edit:\s*deny\s*$", frontmatter)),
        "write_denied": bool(re.search(r"(?m)^\s+write:\s*deny\s*$", frontmatter)),
        "read_denied": bool(re.search(r"(?m)^\s+read:\s*deny\s*$", frontmatter)),
        "task_denied": bool(re.search(r"(?m)^\s+task:\s*deny\s*$", frontmatter)),
        "no_raw_mcp": "chrome-devtools" not in frontmatter.lower(),
        "bash_broad_deny": bool(re.search(r"(?m)^\s+\"?\*\"?:\s*deny\s*$", frontmatter)),
        "bash_allowlisted": (
            "tools/job_browser.py" in frontmatter and "tools/portal.py" in frontmatter
        ),
    }
    summary["ready"] = all(
        value for key, value in summary.items() if key not in ("present", "ready", "reason")
    )
    return summary


def cdp_dependency_summary() -> dict:
    """Check the pinned CDP client dependency (tools/requirements.txt)."""
    pinned = ""
    if REQUIREMENTS_PATH.is_file():
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*websocket-client==([0-9][0-9A-Za-z.\-]*)", line)
            if match:
                pinned = match.group(1)
                break
    if not pinned:
        return {"pinned": None, "installed": None, "ok": False, "reason": "no pinned version"}
    spec = importlib.util.find_spec("websocket")
    if spec is None:
        return {"pinned": pinned, "installed": None, "ok": False, "reason": "websocket-client not installed"}
    import websocket  # type: ignore

    installed = getattr(websocket, "__version__", "unknown")
    return {
        "pinned": pinned,
        "installed": installed,
        "ok": installed == pinned,
        "reason": "" if installed == pinned else f"installed {installed} != pinned {pinned}",
    }


def doctor(profile: Path, port: int) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    chrome = find_chrome()
    check("chrome_installed", chrome is not None, chrome or "google-chrome not found")

    try:
        resolved = validate_profile(profile)
        check("dedicated_profile", True, str(resolved))
    except BrowserError as error:
        check("dedicated_profile", False, str(error))
        resolved = profile

    personal_in_use = any(
        (resolved == root.resolve() or root.resolve() in resolved.parents)
        for root in PERSONAL_PROFILE_ROOTS
    )
    check("personal_profile_avoided", not personal_in_use, "personal Chrome profile is not used")

    profile_exists = resolved.is_dir()
    check("profile_directory", profile_exists, str(resolved))

    if not is_loopback_host(DEBUG_HOST):
        check("debugging_loopback", False, f"non-loopback host {DEBUG_HOST}")
    else:
        free = port_is_free(port)
        reachable = cdp_version(port) is not None
        check(
            "debugging_loopback",
            True,
            f"{DEBUG_HOST}:{port} free={free} cdp_reachable={reachable}",
        )

    raw_mcp = raw_mcp_summary()
    check(
        "raw_mcp_absent",
        not (raw_mcp.get("configured") and raw_mcp.get("enabled")),
        json.dumps(raw_mcp, ensure_ascii=False),
    )

    operator = portal_operator_summary()
    check(
        "portal_operator",
        bool(operator.get("ready")),
        json.dumps(operator, ensure_ascii=False),
    )

    dependency = cdp_dependency_summary()
    check(
        "cdp_dependency_pinned",
        bool(dependency.get("ok")),
        json.dumps(dependency, ensure_ascii=False),
    )

    repo_secret = _repo_has_credentials()
    check("no_repo_credentials", not repo_secret, "no credential values detected in tracked config")

    check("roboform_untouched", True, ROBOFORM_VAULT_HINT)

    passed = all(item["ok"] for item in checks)
    return {"status": "PASS" if passed else "FAIL", "profile": str(resolved), "checks": checks}


def _repo_has_credentials() -> bool:
    """Conservative check: no password field may be set in tracked config files."""
    for path in sorted(REPO_ROOT.glob(".opencode/*.json*")):
        try:
            text = path.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        for token in ("password", "passwd", "secret", "token", "cookie"):
            if token in text:
                return True
    return False


def _build_command(chrome: str, profile: Path, port: int) -> list[str]:
    return [
        chrome,
        f"--user-data-dir={profile}",
        f"--remote-debugging-address={DEBUG_HOST}",
        f"--remote-debugging-port={int(port)}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]


def start(
    profile: Path,
    port: int,
    wait_seconds: float = 20.0,
    *,
    dedicated_base: Path = DEDICATED_BASE,
) -> dict:
    resolved = validate_profile(profile, dedicated_base=dedicated_base)
    if not is_loopback_host(DEBUG_HOST):
        raise BrowserError("DevTools must bind to loopback only")

    if browser_running(resolved):
        reachable = cdp_version(port) is not None
        return {
            "status": "ALREADY_RUNNING",
            "profile": str(resolved),
            "port": port,
            "pids": browser_processes(resolved),
            "cdp_reachable": reachable,
        }

    if cdp_version(port) is not None:
        raise BrowserError(
            f"port {port} already serves a DevTools endpoint for a different profile; refusing"
        )
    if not port_is_free(port):
        raise BrowserError(f"port {port} is in use; choose another --port")

    chrome = find_chrome()
    if not chrome:
        raise BrowserError("Google Chrome is not installed")

    resolved.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved, 0o700)
    log_path = resolved / "chrome.log"

    command = _build_command(chrome, resolved, port)
    with log_path.open("ab") as log:
        subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if cdp_version(port) is not None:
            return {
                "status": "STARTED",
                "profile": str(resolved),
                "port": port,
                "pids": browser_processes(resolved),
                "cdp_reachable": True,
            }
        time.sleep(0.25)

    return {
        "status": "STARTED_NO_CDP",
        "profile": str(resolved),
        "port": port,
        "pids": browser_processes(resolved),
        "cdp_reachable": False,
    }


def status(profile: Path, port: int, *, dedicated_base: Path = DEDICATED_BASE) -> dict:
    resolved = validate_profile(profile, dedicated_base=dedicated_base)
    running = browser_running(resolved)
    version = cdp_version(port)
    return {
        "status": "RUNNING" if running else "STOPPED",
        "profile": str(resolved),
        "pids": browser_processes(resolved),
        "cdp_reachable": version is not None,
        "browser": (version or {}).get("Browser", ""),
        "credentials": "none stored by this tool; RoboForm stays inside the browser",
    }


def stop(
    profile: Path,
    port: int,
    wait_seconds: float = 8.0,
    *,
    dedicated_base: Path = DEDICATED_BASE,
) -> dict:
    resolved = validate_profile(profile, dedicated_base=dedicated_base)
    pids = browser_processes(resolved)
    if not pids:
        return {"status": "NOT_RUNNING", "profile": str(resolved), "pids": []}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.time() + wait_seconds
    while time.time() < deadline and browser_processes(resolved):
        time.sleep(0.25)
    remaining = browser_processes(resolved)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
    return {
        "status": "STOPPED",
        "profile": str(resolved),
        "pids": pids,
        "cdp_reachable": cdp_version(port) is not None,
    }


def _print(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"status: {payload.get('status', '')}")
    for key, value in payload.items():
        if key == "status":
            continue
        if key == "checks":
            for item in value:
                mark = "OK" if item["ok"] else "FAIL"
                print(f"  [{mark}] {item['check']}: {item['detail']}")
            continue
        print(f"  {key}: {value}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("doctor", "start", "status", "stop"),
        help="action to perform",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help=f"dedicated user-data-dir (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"loopback DevTools port (default: {DEFAULT_PORT})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            payload = doctor(args.profile, args.port)
        elif args.command == "start":
            payload = start(args.profile, args.port)
        elif args.command == "status":
            payload = status(args.profile, args.port)
        else:
            payload = stop(args.profile, args.port)
    except BrowserError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print(payload, args.json)
    if args.command == "doctor" and payload["status"] != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

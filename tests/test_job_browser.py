"""Guards for the dedicated authenticated Job Search browser launcher.

The launcher is a security boundary: it decides which Chrome profile may hold
authenticated portal sessions, and it exposes the DevTools endpoint. These
tests pin the guardrails that would fail silently otherwise:

- only the dedicated user-data-dir under the tool's own base is accepted;
- the personal Chrome profile is rejected;
- DevTools can never be configured off loopback;
- a foreign DevTools endpoint on the port is never adopted;
- the pinned local MCP connector stays pinned, loopback-only and secret-free.
"""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))

import job_browser  # noqa: E402


class ProfileBoundaryTests(unittest.TestCase):
    def test_dedicated_profile_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "ai-job-search"
            profile = base / "chrome-profile"
            self.assertEqual(
                job_browser.validate_profile(profile, dedicated_base=base),
                profile.resolve(),
            )

    def test_profile_outside_dedicated_base_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "ai-job-search"
            outside = Path(directory) / "somewhere-else" / "chrome-profile"
            with self.assertRaises(job_browser.BrowserError):
                job_browser.validate_profile(outside, dedicated_base=base)

    def test_dedicated_base_itself_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "ai-job-search"
            with self.assertRaises(job_browser.BrowserError):
                job_browser.validate_profile(base, dedicated_base=base)

    def test_personal_chrome_profile_rejected(self):
        personal = Path.home() / ".config" / "google-chrome"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "ai-job-search"
            with self.assertRaises(job_browser.BrowserError):
                job_browser.validate_profile(personal, dedicated_base=base)

    def test_relative_profile_rejected(self):
        with self.assertRaises(job_browser.BrowserError):
            job_browser.validate_profile(Path("chrome-profile"))


class LoopbackTests(unittest.TestCase):
    def test_cdp_url_rejects_non_loopback_host(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com", "::"):
            with self.subTest(host=host):
                with self.assertRaises(job_browser.BrowserError):
                    job_browser.cdp_url(9222, host=host)

    def test_cdp_url_accepts_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                self.assertIn(host, job_browser.cdp_url(9222, host=host))

    def test_launch_command_binds_loopback_only(self):
        command = job_browser._build_command(
            "/usr/bin/google-chrome", Path("/tmp/example/chrome-profile"), 9222
        )
        joined = " ".join(command)
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertIn("--remote-debugging-port=9222", command)
        self.assertIn("--user-data-dir=/tmp/example/chrome-profile", command)
        self.assertNotIn("0.0.0.0", joined)
        for unsafe in ("--disable-web-security", "--no-sandbox", "--ignore-certificate-errors"):
            self.assertNotIn(unsafe, command)

    def test_port_probe_rejects_non_loopback(self):
        with self.assertRaises(job_browser.BrowserError):
            job_browser.port_is_free(9222, host="0.0.0.0")


class ForeignDevToolsTests(unittest.TestCase):
    """`start` must never adopt a DevTools endpoint it did not launch."""

    def test_start_refuses_foreign_cdp_endpoint(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API
                payload = json.dumps({"Browser": "foreign"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / "ai-job-search"
                profile = base / "chrome-profile"
                with self.assertRaises(job_browser.BrowserError) as context:
                    job_browser.start(profile, port, dedicated_base=base)
                self.assertIn("already serves a DevTools endpoint", str(context.exception))
        finally:
            server.shutdown()
            server.server_close()

    def test_browser_process_scan_requires_exact_profile_marker(self):
        # No Chrome is launched here; the scan must simply not match unrelated PIDs.
        fake = Path("/tmp/definitely-not-running/chrome-profile")
        self.assertEqual(job_browser.browser_processes(fake), [])


class ExactProcessMatchingTests(unittest.TestCase):
    """`--user-data-dir` matching must be exact, never a substring."""

    PROFILE = Path("/home/u/.local/share/ai-job-search/chrome-profile")

    def test_exact_single_argument_matches(self):
        argv = ["/opt/google/chrome/chrome", f"--user-data-dir={self.PROFILE}"]
        self.assertTrue(job_browser._matches_profile(argv, self.PROFILE))

    def test_substring_profile2_does_not_match(self):
        argv = ["/opt/google/chrome/chrome", f"--user-data-dir={self.PROFILE}2"]
        self.assertFalse(job_browser._matches_profile(argv, self.PROFILE))

    def test_two_argument_form_matches(self):
        argv = ["/opt/google/chrome/chrome", "--user-data-dir", str(self.PROFILE)]
        self.assertTrue(job_browser._matches_profile(argv, self.PROFILE))

    def test_two_argument_form_lookalike_does_not_match(self):
        argv = ["/opt/google/chrome/chrome", "--user-data-dir", f"{self.PROFILE}2"]
        self.assertFalse(job_browser._matches_profile(argv, self.PROFILE))

    def test_personal_profile_never_matches(self):
        argv = ["/opt/google/chrome/chrome", "--user-data-dir=/home/u/.config/google-chrome/Default"]
        self.assertFalse(job_browser._matches_profile(argv, self.PROFILE))

    def test_chrome_process_title_rewrite_matches(self):
        # Chrome clobbers its argv into one process-title string on Linux.
        title = f"/opt/google/chrome/chrome --user-data-dir={self.PROFILE} --remote-debugging-port=9222"
        self.assertTrue(job_browser._matches_profile([title], self.PROFILE))

    def test_chrome_process_title_rewrite_rejects_lookalike(self):
        title = f"/opt/google/chrome/chrome --user-data-dir={self.PROFILE}2"
        self.assertFalse(job_browser._matches_profile([title], self.PROFILE))

    def test_matching_requires_a_chrome_binary(self):
        self.assertTrue(job_browser._is_chrome_argv0(["/opt/google/chrome/chrome", "x"]))
        self.assertFalse(job_browser._is_chrome_argv0(["/usr/bin/python3", "x"]))


class StopScopeTests(unittest.TestCase):
    """`stop` must kill only exact dedicated-profile processes."""

    def test_stop_kills_only_matching_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "ai-job-search"
            profile = base / "chrome-profile"
            exact = ["/opt/google/chrome/chrome", f"--user-data-dir={profile}"]
            lookalike = ["/opt/google/chrome/chrome", f"--user-data-dir={profile}2"]
            personal = ["/opt/google/chrome/chrome", "--user-data-dir=/home/u/.config/google-chrome/Default"]
            with mock.patch.object(
                job_browser,
                "_iter_proc_argv",
                return_value=[(111, exact), (222, lookalike), (333, personal)],
            ), mock.patch.object(job_browser.os, "kill") as kill, mock.patch.object(
                job_browser, "cdp_version", return_value=None
            ):
                job_browser.stop(profile, 9222, wait_seconds=0, dedicated_base=base)
            killed = {call.args[0] for call in kill.call_args_list}
            self.assertEqual(killed, {111})


class RawMcpAbsenceTests(unittest.TestCase):
    """The unrestricted raw Chrome DevTools MCP must not be available."""

    def test_no_chrome_devtools_mcp_configured(self):
        data = json.loads((REPO / ".opencode" / "opencode.json").read_text(encoding="utf-8"))
        self.assertNotIn("chrome-devtools", json.dumps(data))
        self.assertNotIn("mcp", data)

    def test_raw_mcp_summary_reports_absent(self):
        summary = job_browser.raw_mcp_summary()
        self.assertFalse(summary.get("configured"))
        self.assertFalse(summary.get("enabled"))


class PortalOperatorAgentTests(unittest.TestCase):
    """The portal operator is the only authenticated-browser path."""

    @classmethod
    def setUpClass(cls):
        cls.path = REPO / ".opencode" / "agents" / "portal-operator.md"
        cls.text = cls.path.read_text(encoding="utf-8")

    def test_agent_exists_and_is_subagent(self):
        self.assertTrue(self.path.is_file())
        self.assertRegex(self.text, r"(?m)^mode:\s*subagent\s*$")

    def test_agent_denies_write_read_and_task(self):
        for key in ("edit", "write", "read", "glob", "grep", "list", "task", "webfetch", "websearch"):
            with self.subTest(key=key):
                self.assertRegex(self.text, rf"(?m)^\s+{key}:\s*deny\s*$")

    def test_shell_is_broadly_denied_with_narrow_allowlist(self):
        self.assertRegex(self.text, r'(?m)^\s+"?\*"?:\s*deny\s*$')
        self.assertIn("tools/job_browser.py", self.text)
        self.assertIn("tools/portal.py", self.text)

    def test_no_raw_browser_mcp_in_agent(self):
        front = self.text.split("---", 2)[1].lower()
        self.assertNotIn("chrome-devtools", front)

    def test_operator_summary_ready(self):
        summary = job_browser.portal_operator_summary()
        self.assertTrue(summary["present"])
        self.assertTrue(summary["ready"], summary)


class CdpDependencyTests(unittest.TestCase):
    """The CDP client dependency is pinned, not assumed from the environment."""

    REQUIREMENTS = REPO / "tools" / "requirements.txt"

    def test_requirements_pin_websocket_client(self):
        text = self.REQUIREMENTS.read_text(encoding="utf-8")
        self.assertIn("websocket-client==1.9.0", text)
        self.assertNotIn("websocket-client>=", text)

    def test_dependency_summary_reports_the_pin(self):
        summary = job_browser.cdp_dependency_summary()
        self.assertEqual(summary["pinned"], "1.9.0")
        self.assertIsInstance(summary["ok"], bool)

    def test_config_has_no_secret_fields(self):
        text = (REPO / ".opencode" / "opencode.json").read_text(encoding="utf-8").lower()
        for token in ("password", "passwd", "secret", "bearer", "cookie", "api_key"):
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()

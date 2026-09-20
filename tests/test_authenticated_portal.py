"""Guards for the provider-neutral authenticated portal layer.

The portal layer is a security boundary between an authenticated browser
session and the application workflow. These tests pin the invariants that a
careless edit would silently break: exact hostname matching (no wildcard), the
loss of auth privilege on external redirects, the auth/challenge state machine,
the minimized job schema, the manifest-bound upload set, and the absolute ban
on final submission.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))

import portal  # noqa: E402


class _FakeClient:
    def __init__(self, *, navigate_result=None, evaluate_results=None):
        self.navigate_result = navigate_result or {}
        self.evaluate_results = list(evaluate_results or [])
        self.calls = []

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return dict(self.navigate_result, url=self.navigate_result.get("url", url))

    def evaluate(self, expression, **_):
        self.calls.append(("evaluate", expression[:40]))
        if self.evaluate_results:
            return self.evaluate_results.pop(0)
        return None

    def call(self, method, params=None):
        self.calls.append(("call", method))
        return {}

    def close(self):
        return None


class ExternalNavigationBoundaryTests(unittest.TestCase):
    """The privileged browser must never navigate outside the allowlist."""

    def setUp(self):
        self.portal = portal.get_portal("careercross")
        self.session = portal.PortalSession(portal=self.portal, port=9222)

    def test_direct_external_url_rejected(self):
        with self.assertRaises(portal.ExternalNavigationError):
            self.session.goto("https://careers.example.com/jobs/1")

    def test_lookalike_host_rejected(self):
        for url in (
            "https://careercross.com.evil.example/job/1",
            "https://fakecareercross.com/job/1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(portal.ExternalNavigationError):
                    self.session.goto(url)

    def test_userinfo_spoof_rejected(self):
        with self.assertRaises(portal.ExternalNavigationError):
            self.session.goto("https://careercross.com@evil.example/job/1")

    def test_redirect_to_external_is_stopped(self):
        fake = _FakeClient(navigate_result={"url": "https://evil.example/redirected"})
        with mock.patch.object(portal.PortalSession, "_client", return_value=fake):
            with self.assertRaises(portal.ExternalNavigationError) as context:
                self.session.goto("https://www.careercross.com/en/job/detail-1")
            self.assertIn("left the approved hosts", str(context.exception))


class SafeFormBoundaryTests(unittest.TestCase):
    def test_forbidden_field_names_detected(self):
        for name in ("password", "user_password", "csrf_token", "authenticity_token", "_session", "hidden_field"):
            with self.subTest(name=name):
                self.assertTrue(portal.FORBIDDEN_FIELD_RE.search(name))

    def test_sensitive_labels_detected(self):
        for label in ("Expected Salary", "給与", "年収", "Visa status", "在留資格", "Available start date"):
            with self.subTest(label=label):
                self.assertTrue(portal.SENSITIVE_LABEL_RE.search(label))

    def test_sanitize_rejects_forbidden_field(self):
        session = portal.PortalSession(portal=portal.get_portal("careercross"), port=9222)
        with self.assertRaises(portal.PortalError):
            session._sanitize_field({"identifier": "csrf_token", "name": "csrf_token", "type": "text"})

    def test_sanitize_never_returns_values(self):
        session = portal.PortalSession(portal=portal.get_portal("careercross"), port=9222)
        field = session._sanitize_field(
            {
                "identifier": "full_name",
                "name": "full_name",
                "label": "Full name",
                "type": "text",
                "value_state": "non-empty",
            }
        )
        self.assertNotIn("value", field)
        self.assertEqual(field["value_state"], "non-empty")


class FillGuardTests(unittest.TestCase):
    def setUp(self):
        self.session = portal.PortalSession(portal=portal.get_portal("careercross"), port=9222)

    def _fields(self, **overrides):
        base = {
            "identifier": "full_name",
            "label": "Full name",
            "type": "text",
            "required": False,
            "maxlength": None,
            "options": [],
            "value_state": "empty",
        }
        base.update(overrides)
        return [base]

    def test_unknown_field_identifier_rejected(self):
        with mock.patch.object(portal.PortalSession, "form_snapshot", return_value={"fields": self._fields()}):
            with self.assertRaises(portal.PortalError):
                self.session.fill({"fields": {"nope": "x"}})

    def test_password_and_token_fields_refused(self):
        for identifier in ("password", "csrf_token"):
            with self.subTest(identifier=identifier):
                fields = self._fields(identifier=identifier, label=identifier)
                with mock.patch.object(portal.PortalSession, "form_snapshot", return_value={"fields": fields}):
                    with self.assertRaises(portal.PortalError):
                        self.session.fill({"fields": {identifier: "x"}})

    def test_maxlength_enforced(self):
        fields = self._fields(identifier="pitch", label="Pitch", maxlength="5")
        with mock.patch.object(portal.PortalSession, "form_snapshot", return_value={"fields": fields}):
            with self.assertRaises(portal.PortalError):
                self.session.fill({"fields": {"pitch": "too long"}})

    def test_select_option_enforced(self):
        fields = self._fields(
            identifier="location",
            label="Location",
            type="select",
            options=[{"value": "tokyo", "label": "Tokyo"}],
        )
        with mock.patch.object(portal.PortalSession, "form_snapshot", return_value={"fields": fields}):
            with self.assertRaises(portal.PortalError):
                self.session.fill({"fields": {"location": "osaka"}})

    def test_sensitive_field_requires_confirmation(self):
        fields = self._fields(identifier="salary", label="Expected salary", type="text")
        with mock.patch.object(portal.PortalSession, "form_snapshot", return_value={"fields": fields}):
            result = self.session.fill({"fields": {"salary": "6000000"}})
        self.assertEqual(result["state"], portal.USER_ACTION_REQUIRED)

    def test_unknown_required_field_stops(self):
        fields = [
            {"identifier": "full_name", "label": "Full name", "type": "text", "required": True, "maxlength": None, "options": [], "value_state": "empty"},
            {"identifier": "portfolio", "label": "Portfolio URL", "type": "text", "required": True, "maxlength": None, "options": [], "value_state": "empty"},
        ]
        fake = _FakeClient(evaluate_results=[{"filled": ["full_name"], "refused": []}])
        with mock.patch.object(portal.PortalSession, "form_snapshot", return_value={"fields": fields}), mock.patch.object(
            portal.PortalSession, "_client", return_value=fake
        ):
            result = self.session.fill({"fields": {"full_name": "Alex Example"}})
        self.assertEqual(result["state"], portal.USER_ACTION_REQUIRED)
        self.assertEqual(result["missing_required"], ["portfolio"])


class LoginGuardTests(unittest.TestCase):
    def setUp(self):
        self.session = portal.PortalSession(portal=portal.get_portal("careercross"), port=9222)

    def test_challenge_stops_login(self):
        snapshot = {"signals": {"has_captcha": True}}
        with mock.patch.object(portal.PortalSession, "goto"), mock.patch.object(
            portal.PortalSession, "snapshot", return_value=snapshot
        ):
            result = self.session.login(wait_seconds=0)
        self.assertEqual(result["state"], portal.USER_ACTION_REQUIRED)

    def test_external_form_action_refused(self):
        snapshot = {"signals": {}}
        fake = _FakeClient(evaluate_results=[{"form": True, "action_host": "evil.example", "username_present": True, "password_present": True, "has_submit": True}])
        with mock.patch.object(portal.PortalSession, "goto"), mock.patch.object(
            portal.PortalSession, "snapshot", return_value=snapshot
        ), mock.patch.object(portal.PortalSession, "_client", return_value=fake):
            with self.assertRaises(portal.PortalError):
                self.session.login(wait_seconds=0)

    def test_locked_roboform_asks_user(self):
        snapshot = {"signals": {}}
        fake = _FakeClient(evaluate_results=[{"form": True, "action_host": "www.careercross.com", "username_present": False, "password_present": False, "has_submit": True}])
        with mock.patch.object(portal.PortalSession, "goto"), mock.patch.object(
            portal.PortalSession, "snapshot", return_value=snapshot
        ), mock.patch.object(portal.PortalSession, "_client", return_value=fake):
            result = self.session.login(wait_seconds=0)
        self.assertEqual(result["state"], portal.USER_ACTION_REQUIRED)
        self.assertIn("RoboForm", result["reason"])

    def test_login_result_never_contains_a_value(self):
        self.assertIn("username_present", portal.LOGIN_FORM_JS)
        self.assertIn("password_present", portal.LOGIN_FORM_JS)
        self.assertNotIn("password" + ":", portal.LOGIN_FORM_JS)
        # The probe only reports boolean presence; it never serializes a value.
        self.assertEqual(portal.LOGIN_FORM_JS.count(".value"), 2)


class ContinueGuardTests(unittest.TestCase):
    def setUp(self):
        self.session = portal.PortalSession(portal=portal.get_portal("careercross"), port=9222)

    def test_final_state_never_clicks(self):
        fake = _FakeClient()
        snapshot = {"signals": {"authenticated": True, "has_final_submit": True}}
        with mock.patch.object(portal.PortalSession, "snapshot", return_value=snapshot), mock.patch.object(
            portal.PortalSession, "_client", return_value=fake
        ):
            result = self.session.continue_step()
        self.assertEqual(result["state"], portal.READY_FOR_FINAL_SUBMISSION)
        self.assertFalse(result["clicked"])
        self.assertEqual([call for call in fake.calls if call[0] == "evaluate"], [])

    def test_ambiguous_controls_stop(self):
        controls = [
            {"index": 0, "text": "Next", "intermediate": True, "final": False},
            {"index": 1, "text": "Confirm", "intermediate": True, "final": False},
        ]
        fake = _FakeClient(evaluate_results=[controls])
        snapshot = {"signals": {"authenticated": True}}
        with mock.patch.object(portal.PortalSession, "snapshot", return_value=snapshot), mock.patch.object(
            portal.PortalSession, "_client", return_value=fake
        ):
            result = self.session.continue_step()
        self.assertEqual(result["state"], portal.SUBMISSION_BLOCKED)
        self.assertFalse(result["clicked"])

    def test_no_generic_arbitrary_click_interface(self):
        import inspect

        self.assertFalse(hasattr(portal.PortalSession, "click"))
        parameters = list(inspect.signature(portal.PortalSession.continue_step).parameters)
        self.assertEqual(parameters, ["self"])
        source = (TOOLS / "portal.py").read_text(encoding="utf-8")
        self.assertNotIn("def click(", source)


class UploadBoundaryTests(unittest.TestCase):
    def test_empty_document_list_refused(self):
        session = portal.PortalSession(portal=portal.get_portal("careercross"), port=9222)
        with self.assertRaises(portal.PortalError):
            session.upload([])

    def test_upload_uses_manifest_node_assignment(self):
        source = (TOOLS / "portal.py").read_text(encoding="utf-8")
        self.assertIn("DOM.setFileInputFiles", source)
        # There is no CLI file-path argument for upload; only --manifest/--kind.
        import contextlib
        import io

        parser = portal._parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["upload", "--url", "https://www.careercross.com/en/x", "--file", "x"])


class HostnameBoundaryTests(unittest.TestCase):
    def test_exact_allowed_hosts(self):
        spec = portal.get_portal("careercross")
        self.assertTrue(portal.host_allowed("careercross.com", spec.allowed_hosts))
        self.assertTrue(portal.host_allowed("www.careercross.com", spec.allowed_hosts))
        self.assertTrue(portal.host_allowed("WWW.CareerCross.COM.", spec.allowed_hosts))

    def test_lookalike_hosts_rejected(self):
        spec = portal.get_portal("careercross")
        for host in (
            "careercross.com.evil.example",
            "fakecareercross.com",
            "www.careercross.com.evil.example",
            "careercross.co",
            "careercross.com.br",
            "careercross-com.example",
            "careercrosscom",
        ):
            with self.subTest(host=host):
                self.assertFalse(portal.host_allowed(host, spec.allowed_hosts))

    def test_portal_host_has_privilege(self):
        spec = portal.get_portal("careercross")
        result = portal.classify_url(spec, "https://www.careercross.com/en/job-detail/123456")
        self.assertTrue(result["allowed"])
        self.assertTrue(result["auth_privilege"])
        self.assertTrue(result["is_job"])

    def test_external_redirect_loses_privilege(self):
        spec = portal.get_portal("careercross")
        result = portal.classify_url(spec, "https://careers.example.com/jobs/123")
        self.assertFalse(result["allowed"])
        self.assertFalse(result["auth_privilege"])
        self.assertEqual(result["context"], "external")

    def test_userinfo_spoof_is_not_allowed(self):
        spec = portal.get_portal("careercross")
        result = portal.classify_url(spec, "https://careercross.com@evil.example/job-detail/1")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["host"], "evil.example")

    def test_require_privilege_rejects_external(self):
        spec = portal.get_portal("careercross")
        with self.assertRaises(portal.PortalError):
            portal.require_portal_privilege(spec, "https://evil.example/careercross.com")


class AuthStateTests(unittest.TestCase):
    def test_login_form_requires_auth(self):
        self.assertEqual(
            portal.classify_state({"has_password_input": True}), portal.AUTH_REQUIRED
        )

    def test_captcha_is_user_action(self):
        self.assertEqual(
            portal.classify_state({"has_captcha": True, "authenticated": True}),
            portal.USER_ACTION_REQUIRED,
        )

    def test_cloudflare_challenge_is_user_action(self):
        for marker in ("just a moment", "checking your browser", "cf-chl", "turnstile"):
            with self.subTest(marker=marker):
                self.assertIn(marker, portal.CAPTCHA_MARKERS)
        self.assertIn("challenge-form", portal.JOB_PROBE_JS)
        self.assertIn("cf-turnstile", portal.JOB_PROBE_JS)
        self.assertEqual(
            portal.classify_state({"has_captcha": True}), portal.USER_ACTION_REQUIRED
        )

    def test_final_submit_needs_apply_and_confirm_semantics(self):
        """A generic Send/Submit control must not read as final submission."""
        self.assertNotIn("'送信する'", portal.JOB_PROBE_JS)
        self.assertNotIn("'提出する'", portal.JOB_PROBE_JS)
        self.assertIn("(応募|application|apply)", portal.JOB_PROBE_JS)
        self.assertIn("(確定|送信|submit|confirm|complete)", portal.JOB_PROBE_JS)

    def test_script_urls_do_not_trigger_challenge(self):
        """A Cloudflare-fronted site must not look permanently challenged."""
        self.assertNotIn("'cloudflare'", portal.JOB_PROBE_JS)
        self.assertNotIn('"cloudflare"', portal.JOB_PROBE_JS)

    def test_mfa_is_user_action(self):
        self.assertEqual(
            portal.classify_state({"has_mfa": True, "authenticated": True}),
            portal.USER_ACTION_REQUIRED,
        )

    def test_authenticated_without_forms(self):
        self.assertEqual(
            portal.classify_state({"authenticated": True}), portal.AUTHENTICATED
        )

    def test_authenticated_application_form(self):
        self.assertEqual(
            portal.classify_state({"authenticated": True, "application_form": True}),
            portal.READY_FOR_APPLICATION,
        )

    def test_unauthenticated_application_form_still_requires_auth(self):
        self.assertEqual(
            portal.classify_state({"authenticated": False, "application_form": True}),
            portal.AUTH_REQUIRED,
        )

    def test_review_state(self):
        self.assertEqual(
            portal.classify_state({"authenticated": True, "review_form": True}),
            portal.READY_FOR_REVIEW,
        )

    def test_final_submit_state_is_detected_but_never_clicked(self):
        state = portal.classify_state({"authenticated": True, "has_final_submit": True})
        self.assertEqual(state, portal.READY_FOR_FINAL_SUBMISSION)
        self.assertIn(portal.READY_FOR_FINAL_SUBMISSION, portal.STATES)

    def test_public_state(self):
        self.assertEqual(portal.classify_state({}), portal.PUBLIC)


class FinalSubmitBoundaryTests(unittest.TestCase):
    def test_final_markers_refused(self):
        for action in (
            "Submit Application",
            "confirm and submit",
            "応募を確定",
            "送信する",
            "apply now",
        ):
            with self.subTest(action=action):
                with self.assertRaises(portal.PortalError):
                    portal.assert_no_final_submit(action)

    def test_safe_actions_allowed(self):
        for action in ("open job page", "fill field", "preview application"):
            with self.subTest(action=action):
                portal.assert_no_final_submit(action)

    def test_empty_action_refused(self):
        with self.assertRaises(portal.PortalError):
            portal.assert_no_final_submit("")


class MinimizedJobSchemaTests(unittest.TestCase):
    def test_only_whitelisted_fields_survive(self):
        raw = {
            "company": "Acme",
            "role": "AI Engineer",
            "job_description": "Build things.",
            "email": "candidate@example.com",
            "password": "super-secret",
            "session_token": "abc123",
            "billing": "Visa **** 4242",
            "messages": "unrelated inbox",
            "account_settings": "newsletter opt-in",
        }
        job = portal.filter_job_fields(raw)
        self.assertEqual(set(job), {"company", "role", "job_description"})
        self.assertTrue(set(job).issubset(set(portal.JOB_FIELD_ORDER)))
        for forbidden in ("password", "session_token", "email", "billing", "messages"):
            self.assertNotIn(forbidden, job)

    def test_description_is_capped(self):
        job = portal.filter_job_fields({"job_description": "x" * (portal.MAX_DESCRIPTION_CHARS + 500)})
        self.assertLessEqual(len(job["job_description"]), portal.MAX_DESCRIPTION_CHARS + 20)

    def test_portal_job_id_from_url(self):
        self.assertEqual(
            portal.portal_job_id("https://www.careercross.com/en/job-detail/1234567"),
            "1234567",
        )


class RedactionTests(unittest.TestCase):
    def test_credential_like_values_are_redacted(self):
        text = (
            "password: hunter2\n"
            "Authorization: Bearer abc.def.ghi\n"
            "Cookie: session=deadbeefcafe\n"
            "csrf_token = 0123456789abcdef\n"
            "session_token: synthetictokenvalue\n"
        )
        redacted = portal.redact_secrets(text)
        for secret in ("hunter2", "abc.def.ghi", "deadbeefcafe", "0123456789abcdef", "synthetictokenvalue"):
            self.assertNotIn(secret, redacted)
        self.assertIn("<redacted>", redacted)


class ManifestBoundUploadTests(unittest.TestCase):
    """Upload candidates come from the exact attempt manifest, never a guess."""

    def _fixture(self, root: Path, *, cross_attempt: bool = False) -> Path:
        attempt = "documents/applications/acme_analyst"
        (root / attempt / "drafts" / "japanese").mkdir(parents=True, exist_ok=True)
        docx = root / attempt / "drafts" / "japanese" / "acme_analyst_rirekisho.docx"
        pdf = root / attempt / "drafts" / "japanese" / "acme_analyst_rirekisho.pdf"
        docx.write_bytes(b"docx-bytes")
        pdf.write_bytes(b"pdf-bytes")
        other = "documents/applications/other_attempt/drafts/japanese/other.docx"
        (root / other).parent.mkdir(parents=True, exist_ok=True)
        (root / other).write_bytes(b"other-bytes")
        working = f"documents/applications/other_attempt/drafts/japanese/other.docx" if cross_attempt else docx.relative_to(root).as_posix()
        manifest = {
            "schema_version": 1,
            "application_id": "acme_analyst",
            "application_dir": attempt,
            "company": "Acme",
            "role": "Analyst",
            "route": "japanese",
            "documents": [
                {
                    "kind": "rirekisho",
                    "working_path": working,
                    "pdf_path": pdf.relative_to(root).as_posix(),
                }
            ],
        }
        path = root / attempt / portal.MANIFEST_NAME
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_valid_manifest_returns_paths_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root)
            with mock.patch.object(portal, "REPO_ROOT", root):
                entries = portal.verify_upload_documents(manifest)
            self.assertTrue(entries)
            for entry in entries:
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                self.assertIn("acme_analyst", entry["path"])

    def test_cross_attempt_document_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root, cross_attempt=True)
            with mock.patch.object(portal, "REPO_ROOT", root):
                with self.assertRaises(portal.PortalError):
                    portal.verify_upload_documents(manifest)

    def test_application_id_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._fixture(root)
            with mock.patch.object(portal, "REPO_ROOT", root):
                with self.assertRaises(portal.PortalError):
                    portal.verify_upload_documents(
                        manifest, expected_application_id="some_other_attempt"
                    )

    def test_missing_manifest_refused(self):
        with self.assertRaises(portal.PortalError):
            portal.verify_upload_documents(Path("/nonexistent/application-output.json"))


class NotificationTests(unittest.TestCase):
    """USER_ACTION_REQUIRED raises a sanitized desktop alert or terminal fallback."""

    def test_notify_send_used_when_available(self):
        with mock.patch.object(portal.shutil, "which", return_value="/usr/bin/notify-send"), mock.patch.object(
            portal.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(returncode=0)
            result = portal.send_desktop_notification(portal.action_body_for("captcha"))
        self.assertEqual(result, "notify-send")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "notify-send")
        self.assertIn(portal.NOTIFICATION_TITLE, argv)
        self.assertIn("Cloudflare/CAPTCHA", argv[-1])

    def test_missing_notify_send_degrades_to_terminal(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with mock.patch.object(portal.shutil, "which", return_value=None), contextlib.redirect_stdout(buffer):
            result = portal.send_desktop_notification(portal.DEFAULT_ACTION_BODY)
        self.assertEqual(result, "terminal")
        self.assertIn("\a", buffer.getvalue())
        self.assertIn("USER_ACTION_REQUIRED", buffer.getvalue())

    def test_notify_send_failure_falls_back(self):
        import contextlib
        import io

        with mock.patch.object(portal.shutil, "which", return_value="/usr/bin/notify-send"), mock.patch.object(
            portal.subprocess, "run"
        ) as run, contextlib.redirect_stdout(io.StringIO()):
            run.return_value = mock.Mock(returncode=1)
            result = portal.send_desktop_notification(portal.DEFAULT_ACTION_BODY)
        self.assertEqual(result, "terminal")

    def test_notification_bodies_are_sanitized(self):
        for blocker, body in portal.ACTION_BODIES.items():
            with self.subTest(blocker=blocker):
                lowered = body.lower()
                for forbidden in ("password", "username", "cookie", "token", "session", "csrf", "http", "@"):
                    self.assertNotIn(forbidden, lowered)
        self.assertNotIn("password", portal.NOTIFICATION_TITLE.lower())

    def test_action_bodies_name_the_right_action(self):
        self.assertIn("Cloudflare/CAPTCHA", portal.action_body_for("captcha"))
        self.assertIn("MFA/passkey", portal.action_body_for("mfa"))
        self.assertIn("RoboForm", portal.action_body_for("roboform"))


class _FakeWaitClient:
    def __init__(self, login_form=None):
        self.login_form = login_form
        self.calls = []

    def call(self, method, params=None):
        self.calls.append(("call", method))
        return {}

    def evaluate(self, expression, **_):
        self.calls.append(("evaluate", expression[:40]))
        return self.login_form


class _FakeWaitSession:
    def __init__(self, signals_sequence, *, login_form=None, login_state=portal.AUTHENTICATED):
        self.signals_sequence = list(signals_sequence)
        self.client = _FakeWaitClient(login_form=login_form)
        self.login_calls = 0
        self.login_state = login_state
        self.portal = portal.get_portal("careercross")

    def snapshot(self):
        signals = self.signals_sequence[0]
        if len(self.signals_sequence) > 1:
            self.signals_sequence.pop(0)
        return {"signals": signals}

    def _client(self):
        return self.client

    def login(self):
        self.login_calls += 1
        return {"state": self.login_state}


class WaitResumeTests(unittest.TestCase):
    def test_challenge_clearance_resumes_automatically(self):
        session = _FakeWaitSession([{"has_captcha": True}, {"has_captcha": True}, {}])
        with mock.patch.object(portal, "send_desktop_notification", return_value="notify-send") as notify:
            result = portal.wait_for_user_action(session, timeout=5, interval=0.5)
        self.assertTrue(result["resumed"])
        self.assertEqual(notify.call_count, 1)

    def test_timeout_returns_clear_state(self):
        session = _FakeWaitSession([{"has_captcha": True}])
        with mock.patch.object(portal, "send_desktop_notification", return_value="notify-send"):
            result = portal.wait_for_user_action(session, timeout=0.1, interval=0.5)
        self.assertEqual(result["state"], portal.USER_ACTION_REQUIRED_TIMEOUT)
        self.assertFalse(result["resumed"])

    def test_challenge_is_never_clicked(self):
        session = _FakeWaitSession([{"has_captcha": True}])
        with mock.patch.object(portal, "send_desktop_notification", return_value="notify-send"):
            portal.wait_for_user_action(session, timeout=0.1, interval=0.5)
        methods = [call[1] for call in session.client.calls]
        self.assertNotIn("Runtime.evaluate", methods)
        self.assertIn("Page.bringToFront", methods)

    def test_roboform_populated_triggers_safe_login_once(self):
        login_form = {
            "form": True,
            "action_host": "www.careercross.com",
            "username_present": True,
            "password_present": True,
            "has_submit": True,
        }
        session = _FakeWaitSession(
            [{"has_password_input": True}, {"has_password_input": True}],
            login_form=login_form,
        )
        with mock.patch.object(portal, "send_desktop_notification", return_value="notify-send"):
            result = portal.wait_for_user_action(session, timeout=5, interval=0.5)
        self.assertEqual(session.login_calls, 1)
        self.assertTrue(result["resumed"])

    def test_locked_roboform_does_not_click(self):
        session = _FakeWaitSession([{"has_password_input": True}], login_form={"form": False})
        with mock.patch.object(portal, "send_desktop_notification", return_value="notify-send"):
            result = portal.wait_for_user_action(session, timeout=0.1, interval=0.5)
        self.assertEqual(result["state"], portal.USER_ACTION_REQUIRED_TIMEOUT)
        self.assertEqual(session.login_calls, 0)

    def test_already_clear_does_not_notify(self):
        session = _FakeWaitSession([{}])
        with mock.patch.object(portal, "send_desktop_notification") as notify:
            result = portal.wait_for_user_action(session, timeout=5, interval=0.5)
        self.assertTrue(result["resumed"])
        notify.assert_not_called()

    def test_wait_cli_exists(self):
        parsed = portal._parser().parse_args(["wait-user-action", "--timeout", "5"])
        self.assertEqual(parsed.command, "wait-user-action")
        self.assertEqual(parsed.timeout, 5.0)


if __name__ == "__main__":
    unittest.main()

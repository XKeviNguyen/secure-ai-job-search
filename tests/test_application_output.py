import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "application_output.py"
APPLY = REPO / ".claude" / "commands" / "apply.md"
ROUTING = (
    REPO
    / ".claude"
    / "skills"
    / "job-application-assistant"
    / "10-application-output-routing.md"
)
ADD_TEMPLATE = REPO / ".claude" / "commands" / "add-template.md"
OUTCOME = REPO / ".claude" / "commands" / "outcome.md"
INTERVIEW = REPO / ".claude" / "commands" / "interview.md"
GMAIL_SYNC = REPO / ".claude" / "commands" / "gmail-sync.md"
NOTION_SYNC = REPO / ".claude" / "commands" / "notion-sync.md"
DOCUMENTS_README = REPO / "documents" / "README.md"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ApplicationOutputRoutingTests(unittest.TestCase):
    def run_tool(self, repo_root: Path, *args: str, expected: int = 0):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--repo-root", str(repo_root), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_japanese_route_has_two_distinct_word_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_tool(
                Path(directory),
                "plan",
                "--market",
                "japanese",
                "--company",
                "Acme Japan",
                "--role",
                "SOC Analyst",
            )
            plan = json.loads(result.stdout)
        self.assertEqual(plan["route"], "japanese")
        self.assertEqual(
            [document["kind"] for document in plan["documents"]],
            ["rirekisho", "shokumukeirekisho"],
        )
        self.assertTrue(
            all(document["working_path"].endswith(".docx") for document in plan["documents"])
        )
        self.assertNotIn("cover_letter", json.dumps(plan))

    def test_english_route_has_one_resume_and_uses_application_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_tool(
                Path(directory),
                "plan",
                "--market",
                "international",
                "--company",
                "Example & Co.",
                "--role",
                "AI Trainer / Evaluator",
            )
            plan = json.loads(result.stdout)
        self.assertEqual(plan["route"], "english_international")
        self.assertEqual(len(plan["documents"]), 1)
        self.assertEqual(plan["documents"][0]["kind"], "english_resume")
        self.assertTrue(
            plan["documents"][0]["working_path"].startswith(
                "documents/applications/example_co_ai_trainer_evaluator/drafts/english/"
            )
        )

    def test_application_id_drops_punctuation_per_repository_convention(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_tool(
                Path(directory),
                "plan",
                "--market",
                "english",
                "--company",
                "Novo Nordisk A/S",
                "--role",
                "Data & AI Lead",
            )
            plan = json.loads(result.stdout)
        self.assertEqual(plan["application_id"], "novo_nordisk_as_data_ai_lead")

    def test_unknown_market_fails_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_tool(
                Path(directory),
                "plan",
                "--market",
                "ambiguous",
                "--company",
                "Acme",
                "--role",
                "Analyst",
                expected=2,
            )
        self.assertIn("unknown market", result.stderr)

    def test_remote_is_not_a_document_route_alias(self):
        """Location mode is not a document-market identity: a remote role at a
        Japanese employer can still require 履歴書/職務経歴書, so the route must
        be resolved by employer convention before the path tool is called."""
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_tool(
                Path(directory),
                "plan",
                "--market",
                "remote",
                "--company",
                "Acme",
                "--role",
                "Analyst",
                expected=2,
            )
        self.assertIn("unknown market", result.stderr)

    def test_japanese_market_still_resolves_to_japanese(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = json.loads(
                self.run_tool(
                    Path(directory),
                    "plan",
                    "--market",
                    "japanese",
                    "--company",
                    "Acme",
                    "--role",
                    "Analyst",
                ).stdout
            )
        self.assertEqual(plan["route"], "japanese")

    def test_international_market_still_resolves_to_english(self):
        for market in ("international", "english", "english_international"):
            with self.subTest(market=market):
                with tempfile.TemporaryDirectory() as directory:
                    plan = json.loads(
                        self.run_tool(
                            Path(directory),
                            "plan",
                            "--market",
                            market,
                            "--company",
                            "Acme",
                            "--role",
                            "Analyst",
                        ).stdout
                    )
                self.assertEqual(plan["route"], "english_international")

    @staticmethod
    def _supports_symlinks(root: Path) -> bool:
        link = root / "symlink-probe"
        try:
            os.symlink(root / "symlink-probe-target", link)
        except (OSError, NotImplementedError):
            return False
        finally:
            if os.path.islink(link):
                os.unlink(link)
        return True

    def test_dangling_base_symlink_is_treated_as_occupied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if not self._supports_symlinks(root):
                self.skipTest("symlinks unsupported on this platform")
            applications = root / "documents" / "applications"
            applications.mkdir(parents=True)
            escape_target = root / "escape_target"
            os.symlink(escape_target, applications / "acme_analyst")

            plan = json.loads(
                self.run_tool(
                    root,
                    "plan",
                    "--market",
                    "japanese",
                    "--company",
                    "Acme",
                    "--role",
                    "Analyst",
                ).stdout
            )
            self.assertEqual(plan["application_id"], "acme_analyst_2")
            self.assertEqual(plan["attempt"], 2)
            self.assertFalse(
                escape_target.exists(),
                "a dangling symlink must not become a write target",
            )

    def test_dangling_numbered_attempt_symlink_is_treated_as_occupied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if not self._supports_symlinks(root):
                self.skipTest("symlinks unsupported on this platform")
            applications = root / "documents" / "applications"
            applications.mkdir(parents=True)
            (applications / "acme_analyst").mkdir()
            os.symlink(root / "escape_target", applications / "acme_analyst_2")

            plan = json.loads(
                self.run_tool(
                    root,
                    "plan",
                    "--market",
                    "japanese",
                    "--company",
                    "Acme",
                    "--role",
                    "Analyst",
                ).stdout
            )
            self.assertEqual(plan["application_id"], "acme_analyst_3")
            self.assertEqual(plan["attempt"], 3)
            self.assertFalse((root / "escape_target").exists())

    def test_prepare_copies_sources_and_preserves_original_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "private-sources"
            sources.mkdir()
            rirekisho = sources / "rirekisho.docx"
            shokumu = sources / "shokumukeirekisho.docx"
            rirekisho.write_bytes(b"fixture-rirekisho")
            shokumu.write_bytes(b"fixture-shokumukeirekisho")
            before = {rirekisho: sha256(rirekisho), shokumu: sha256(shokumu)}

            result = self.run_tool(
                root,
                "prepare",
                "--market",
                "japan",
                "--company",
                "Acme",
                "--role",
                "Analyst",
                "--source",
                f"rirekisho={rirekisho}",
                "--source",
                f"shokumukeirekisho={shokumu}",
            )
            manifest = json.loads(result.stdout)

            self.assertEqual(before[rirekisho], sha256(rirekisho))
            self.assertEqual(before[shokumu], sha256(shokumu))
            for document in manifest["documents"]:
                output = root / document["working_path"]
                self.assertTrue(output.is_file())
                self.assertEqual(document["source_sha256"], document["prepared_sha256"])
            manifest_path = root / manifest["manifest_path"]
            self.assertTrue(manifest_path.is_file())

            verify = self.run_tool(
                root, "verify-sources", str(manifest_path)
            )
            self.assertTrue(json.loads(verify.stdout)["all_sources_unchanged"])

    def test_prepare_allocates_numbered_attempts_without_overwriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "rirekisho.docx"
            second = root / "shokumukeirekisho.docx"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            arguments = (
                "prepare",
                "--market",
                "japanese",
                "--company",
                "Acme",
                "--role",
                "Analyst",
                "--source",
                f"rirekisho={first}",
                "--source",
                f"shokumukeirekisho={second}",
            )
            original = json.loads(self.run_tool(root, *arguments).stdout)
            original_manifest = root / original["manifest_path"]
            original_manifest_hash = sha256(original_manifest)
            original_document_hashes = {
                item["kind"]: sha256(root / item["working_path"])
                for item in original["documents"]
            }

            second_attempt = json.loads(self.run_tool(root, *arguments).stdout)
            third_attempt = json.loads(self.run_tool(root, *arguments).stdout)

            self.assertEqual(original["application_id"], "acme_analyst")
            self.assertEqual(original["attempt"], 1)
            self.assertEqual(second_attempt["application_id"], "acme_analyst_2")
            self.assertEqual(second_attempt["attempt"], 2)
            self.assertEqual(third_attempt["application_id"], "acme_analyst_3")
            self.assertEqual(third_attempt["attempt"], 3)
            self.assertEqual(sha256(original_manifest), original_manifest_hash)
            for item in original["documents"]:
                self.assertEqual(
                    sha256(root / item["working_path"]),
                    original_document_hashes[item["kind"]],
                )

    def test_plan_uses_lowest_free_attempt_folder_and_keeps_base_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            applications = root / "documents" / "applications"
            (applications / "acme_analyst").mkdir(parents=True)
            (applications / "acme_analyst_2").mkdir()
            result = self.run_tool(
                root,
                "plan",
                "--market",
                "japanese",
                "--company",
                "Acme",
                "--role",
                "Analyst",
            )
            plan = json.loads(result.stdout)
        self.assertEqual(plan["base_application_id"], "acme_analyst")
        self.assertEqual(plan["application_id"], "acme_analyst_3")
        self.assertEqual(plan["attempt"], 3)
        self.assertTrue(
            plan["manifest_path"].endswith("acme_analyst_3/application-output.json")
        )


class SourceVerificationFailClosedTests(unittest.TestCase):
    """verify-sources must validate the route's complete document set.

    A manifest that names only one of the two Japanese masters - or none, or a
    truncated JSON file - must never report ``all_sources_unchanged: true``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_verify(self, manifest: Path):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--repo-root",
                str(self.root),
                "verify-sources",
                str(manifest),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def source(self, name: str, content: bytes = b"source-bytes") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def manifest(self, documents, route="japanese") -> Path:
        app = self.root / "documents" / "applications" / "acme_analyst"
        app.mkdir(parents=True, exist_ok=True)
        path = app / "application-output.json"
        path.write_text(
            json.dumps({"schema_version": 1, "route": route, "documents": documents}),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def record(kind, source: Path, *, with_path=True, with_hash=True) -> dict:
        entry = {"kind": kind}
        if with_path:
            entry["source_path"] = str(source)
        if with_hash:
            entry["source_sha256"] = sha256(source)
        return entry

    def test_valid_complete_japanese_manifest_passes(self):
        rirekisho = self.source("rirekisho.docx")
        shokumu = self.source("shokumukeirekisho.docx")
        manifest = self.manifest(
            [
                self.record("rirekisho", rirekisho),
                self.record("shokumukeirekisho", shokumu),
            ]
        )
        result = self.run_verify(manifest)
        self.assertTrue(result["all_sources_unchanged"])
        self.assertEqual(result["expected_kinds"], ["rirekisho", "shokumukeirekisho"])
        self.assertEqual(result["errors"], [])

    def test_valid_complete_english_manifest_passes(self):
        resume = self.source("resume.tex")
        manifest = self.manifest(
            [self.record("english_resume", resume)],
            route="english_international",
        )
        result = self.run_verify(manifest)
        self.assertTrue(result["all_sources_unchanged"])
        self.assertEqual(result["expected_kinds"], ["english_resume"])

    def test_missing_source_path_fails_closed(self):
        rirekisho = self.source("rirekisho.docx")
        shokumu = self.source("shokumukeirekisho.docx")
        manifest = self.manifest(
            [
                self.record("rirekisho", rirekisho, with_path=False),
                self.record("shokumukeirekisho", shokumu),
            ]
        )
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])
        entry = next(s for s in result["sources"] if s["kind"] == "rirekisho")
        self.assertEqual(entry["error"], "missing source_path")
        self.assertFalse(entry["unchanged"])

    def test_missing_source_sha256_fails_closed(self):
        rirekisho = self.source("rirekisho.docx")
        shokumu = self.source("shokumukeirekisho.docx")
        manifest = self.manifest(
            [
                self.record("rirekisho", rirekisho),
                self.record("shokumukeirekisho", shokumu, with_hash=False),
            ]
        )
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])
        entry = next(
            s for s in result["sources"] if s["kind"] == "shokumukeirekisho"
        )
        self.assertEqual(entry["error"], "missing source_sha256")

    def test_missing_one_japanese_document_fails_closed(self):
        rirekisho = self.source("rirekisho.docx")
        manifest = self.manifest([self.record("rirekisho", rirekisho)])
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])
        self.assertIn(
            "shokumukeirekisho",
            " ".join(result["errors"]),
            "the complete Japanese set must be required, not just the records present",
        )

    def test_empty_documents_list_fails_closed(self):
        manifest = self.manifest([])
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])
        self.assertEqual(len(result["sources"]), len(result["expected_kinds"]))
        self.assertTrue(all(not entry["unchanged"] for entry in result["sources"]))

    def test_hash_mismatch_fails_closed(self):
        rirekisho = self.source("rirekisho.docx")
        shokumu = self.source("shokumukeirekisho.docx")
        manifest = self.manifest(
            [
                self.record("rirekisho", rirekisho),
                self.record("shokumukeirekisho", shokumu),
            ]
        )
        shokumu.write_bytes(b"tampered after the manifest was written")
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])

    def test_missing_route_fails_closed(self):
        rirekisho = self.source("rirekisho.docx")
        app = self.root / "documents" / "applications" / "acme_analyst"
        app.mkdir(parents=True, exist_ok=True)
        manifest = app / "application-output.json"
        manifest.write_text(
            json.dumps(
                {"schema_version": 1, "documents": [self.record("rirekisho", rirekisho)]}
            ),
            encoding="utf-8",
        )
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])
        self.assertTrue(any("route" in error for error in result["errors"]))

    def test_unparseable_manifest_fails_closed(self):
        app = self.root / "documents" / "applications" / "acme_analyst"
        app.mkdir(parents=True, exist_ok=True)
        manifest = app / "application-output.json"
        manifest.write_text('{"route": "japanese", "documents": [', encoding="utf-8")
        result = self.run_verify(manifest)
        self.assertFalse(result["all_sources_unchanged"])
        self.assertTrue(result["errors"])


class ApplicationOutputSpecificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apply = APPLY.read_text(encoding="utf-8")
        cls.routing = ROUTING.read_text(encoding="utf-8")
        cls.add_template = ADD_TEMPLATE.read_text(encoding="utf-8")
        cls.outcome = OUTCOME.read_text(encoding="utf-8")
        cls.interview = INTERVIEW.read_text(encoding="utf-8")
        cls.gmail_sync = GMAIL_SYNC.read_text(encoding="utf-8")
        cls.notion_sync = NOTION_SYNC.read_text(encoding="utf-8")
        cls.documents_readme = DOCUMENTS_README.read_text(encoding="utf-8")

    def test_fit_approval_precedes_route_and_file_creation(self):
        self.assertLess(
            self.apply.index("## Step 1 Evaluate Fit and Stop for Approval"),
            self.apply.index("## Step 2 Resolve the Application Output Route"),
        )
        self.assertIn("Do not create an application folder", self.apply)

    def test_apply_accepts_a_local_dry_run_posting_file(self):
        step_zero = self.apply.split(
            "## Step 0: Parse Input and Preserve the Posting", 1
        )[1].split("## Step 1", 1)[0]
        self.assertIn("readable local file path", step_zero)
        self.assertIn("read that file as the posting", step_zero)

    def test_japanese_route_requires_two_docx_copies_not_moderncv(self):
        japanese = self.apply.split("### Route A Japanese employer", 1)[1].split(
            "### Route B", 1
        )[0]
        self.assertIn("履歴書", japanese)
        self.assertIn("職務経歴書", japanese)
        self.assertIn("Do not combine them", japanese)
        self.assertIn("substitute moderncv/LaTeX", japanese)
        self.assertIn("minimal OOXML patches", japanese)

    def test_japanese_route_pins_documents_skill_and_bundled_renderer(self):
        for requirement in (
            "official documents skill",
            "bundled LibreOffice",
            "never `/usr/bin/libreoffice`",
            "verify-sources",
            "all_sources_unchanged: true",
        ):
            self.assertIn(requirement, self.apply)

    def test_english_route_keeps_template_and_ats_workflow(self):
        english = self.apply.split("### Route B English or international employer", 1)[1]
        self.assertIn("05-cv-templates.md", english)
        self.assertIn("active template", english)
        self.assertIn("tools/verify_pdf.py", english)
        self.assertIn("Never add a genuine gap as a keyword", english)

    def test_workflow_stops_without_submission(self):
        self.assertIn("have not been submitted", self.apply)
        self.assertIn("Do not open a portal, upload files, send email, or submit", self.apply)
        self.assertIn("state: drafted_for_review", self.apply)

    def test_company_research_cache_rules_survive_the_extension(self):
        research = self.apply.split("### 1. Research the Company", 1)[1].split(
            "### 2.", 1
        )[0]
        self.assertIn("check the cache", research)
        self.assertIn("company_research/", research)
        self.assertIn("still applies", research)
        self.assertRegex(research, r"write.*company_research/")

    def test_add_template_does_not_absorb_japanese_document_family(self):
        self.assertIn("does not register or activate Japanese", self.add_template)
        self.assertIn("10-application-output-routing.md", self.add_template)

    def test_manifest_is_authoritative_and_shokumukeirekisho_is_not_cover_letter(self):
        self.assertIn("authoritative list of document kinds and paths", self.routing)
        self.assertIn("Never put 職務経歴書 in `cover_letter_file`", self.routing)

    def test_apply_does_not_duplicate_the_canonical_routing_table(self):
        """AGENTS.md's single-source-of-truth rule: the routing decision lives
        in one file, and /apply points at it instead of restating it."""
        step_two = self.apply.split(
            "## Step 2 Resolve the Application Output Route", 1
        )[1].split("## Step 3", 1)[0]
        self.assertIn("10-application-output-routing.md", step_two)
        self.assertRegex(step_two, r"(?i)single source of truth")
        for duplicated in (
            "producing separate 履歴書 and 職務経歴書",
            "producing one English resume",
            "Explicit employer format requirement",
            "Ambiguous employer market/format",
        ):
            self.assertNotIn(
                duplicated,
                step_two,
                "the canonical routing rules must not be duplicated in /apply - "
                "they can drift from 10-application-output-routing.md",
            )
        self.assertIn("Routing Decision", self.routing)

    def test_reapplication_attempts_preserve_prior_archives(self):
        for text in (self.routing, self.documents_readme):
            self.assertIn("_2", text)
            self.assertIn("_3", text)
            self.assertRegex(text, r"(?i)never (?:reuse it or )?overwrite")
        for protected_name in (
            "posting",
            "manifest",
            "drafts",
            "submitted files",
            "outcome",
        ):
            self.assertIn(protected_name, self.routing)

    def test_apply_records_the_exact_prepare_manifest_for_each_attempt(self):
        tracker_step = self.apply.split("### Step 6b: Record the Application", 1)[1]
        self.assertIn("using the exact relative path returned by `prepare`", tracker_step)
        self.assertIn("Never derive this value from company and role", tracker_step)
        self.assertIn("newly allocated attempt", tracker_step)
        self.assertIn("even when another open or final row has the same company and role", tracker_step)
        self.assertIn("Preserve every prior attempt row", tracker_step)

    def test_outcome_and_interview_resolve_attempt_from_tracker_manifest(self):
        for command in (self.outcome, self.interview):
            self.assertIn("application_manifest=<relative path>", command)
            self.assertIn("application-output.json", command)
            self.assertIn("Do not derive or guess the base company-role folder", command)
            self.assertIn("legacy row with no manifest pointer", command)

    def test_other_archive_consumers_do_not_regress_to_base_folder_guessing(self):
        for command in (self.gmail_sync, self.notion_sync):
            self.assertIn("application_manifest=<relative path>", command)
            self.assertIn("application-output.json", command)
            self.assertRegex(command, r"Do not (?:derive or )?guess the base")
            self.assertIn("legacy", command)


if __name__ == "__main__":
    unittest.main()

"""Regression guards for per-attempt identity across outcome/html-report/notion.

Re-applications to the same company and role are separate attempts
(`acme_analyst`, `acme_analyst_2`, ...). Three markdown specs must key on the
tracker row's exact `application_manifest` / allocated `application_id` rather
than a fuzzy company+role match, or the wrong attempt's outcome, stages, or
Notion page gets attributed to the wrong row:

- `/outcome` archives the exact confirmed submitted files (DOCX or PDF), not a
  PDF-only default, and never relabels 職務経歴書 as a cover letter.
- `/html-report` joins `outcome.md` to tracker rows by attempt identity first.
- `/notion-sync` derives a distinct Key per attempt.

The spec IS the implementation, so these are structural assertions plus a small
executable copy of each documented resolution rule to demonstrate separation.
"""
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
OUTCOME = REPO / ".claude" / "commands" / "outcome.md"
HTML_REPORT = REPO / ".claude" / "commands" / "html-report.md"
NOTION_SYNC = REPO / ".claude" / "commands" / "notion-sync.md"
INTERVIEW = REPO / ".claude" / "commands" / "interview.md"
SETUP = REPO / ".claude" / "commands" / "setup.md"


def section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = re.search(r"^#{1,6} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


MANIFEST_RE = re.compile(r"application_manifest=([^\s,;`\"']+)")


def attempt_application_id(notes: str) -> str | None:
    """The documented attempt-resolution rule: manifest pointer first.

    Returns the attempt directory name (`acme_analyst_2`) when the row's notes
    carry an `application_manifest=<relative path>` token pointing at an
    `application-output.json` under documents/applications/; otherwise None so
    the caller falls back to the legacy company+role key.
    """
    match = MANIFEST_RE.search(notes or "")
    if not match:
        return None
    manifest_path = match.group(1).strip()
    if not manifest_path.endswith("application-output.json"):
        return None
    return Path(manifest_path).parent.name


def legacy_key(company: str, role: str) -> str:
    return f"{company}_{role}".lower().replace(" ", "_")


class OutcomeArchivesExactSubmittedSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.step3 = section(
            OUTCOME.read_text(encoding="utf-8"),
            "## Step 3: Archive the Application Materials",
        )

    def test_spec_requires_the_exact_confirmed_files(self):
        self.assertIn("EXACT files the user confirms were actually submitted", self.step3)
        self.assertIn("never a PDF-only default", self.step3)

    def test_spec_lists_docx_and_pdf_as_candidates(self):
        self.assertIn("working_path", self.step3)
        self.assertIn("pdf_path", self.step3)
        self.assertIn("DOCX", self.step3)

    def test_spec_records_kind_path_and_hash(self):
        self.assertIn("submitted_documents", self.step3)
        self.assertIn("{kind, path, sha256}", self.step3)

    def test_spec_keeps_japanese_document_kinds_distinct(self):
        self.assertIn("separate 履歴書 and 職務経歴書 records", self.step3)
        self.assertIn("職務経歴書 is never relabeled as a cover letter", self.step3)

    def test_spec_allows_ancillary_cover_letter_without_relabeling(self):
        self.assertIn("cover_letter` kind", self.step3)

    def test_pdf_only_wording_is_gone(self):
        self.assertNotIn(
            "manifest-listed PDFs were actually sent",
            self.step3,
            "the PDF-only restriction is the bug - a confirmed DOCX or ancillary "
            "cover letter must be archivable and recorded",
        )


class HtmlReportJoinsByAttemptIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.command = HTML_REPORT.read_text(encoding="utf-8")
        cls.step1 = section(cls.command, "## Step 1: Collect Data")

    def test_manifest_pointer_is_the_primary_join(self):
        self.assertIn("application_manifest", self.step1)
        self.assertIn("attempt identity first", self.step1)
        self.assertIn("application_id", self.step1)

    def test_company_role_is_legacy_fallback_only(self):
        self.assertIn("legacy fallback only", self.step1)

    def test_old_fuzzy_primary_join_is_gone(self):
        self.assertNotIn(
            "Merge this into the matching tracker row by company+role fuzzy match",
            self.step1,
        )

    def test_two_attempts_with_same_company_role_map_to_distinct_archives(self):
        first_id = attempt_application_id(
            "application_manifest=documents/applications/acme_analyst/application-output.json"
        )
        second_id = attempt_application_id(
            "application_manifest=documents/applications/acme_analyst_2/application-output.json"
        )
        self.assertEqual(first_id, "acme_analyst")
        self.assertEqual(second_id, "acme_analyst_2")
        self.assertNotEqual(first_id, second_id)

    def test_legacy_rows_without_a_pointer_fall_back_to_company_role(self):
        self.assertIsNone(attempt_application_id("no manifest here"))
        self.assertEqual(
            legacy_key("Acme", "Data Analyst"), "acme_data_analyst"
        )


class NotionSyncKeysByAttempt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.command = NOTION_SYNC.read_text(encoding="utf-8")
        cls.step2 = section(cls.command, "## Step 2: Build the Sync Set")

    def test_key_is_derived_from_the_attempt(self):
        self.assertIn("application_manifest", self.step2)
        self.assertIn("application_id", self.step2)
        self.assertIn("Attempt identity is the Key", self.step2)

    def test_spec_shows_two_distinct_attempt_keys(self):
        self.assertIn("acme_analyst", self.step2)
        self.assertIn("acme_analyst_2", self.step2)
        self.assertIn("distinct Notion records", self.step2)

    def test_legacy_rows_keep_the_legacy_key(self):
        self.assertIn("legacy", self.step2)

    def test_two_attempts_derive_distinct_keys(self):
        first = attempt_application_id(
            "application_manifest=documents/applications/acme_analyst/application-output.json"
        )
        second = attempt_application_id(
            "application_manifest=documents/applications/acme_analyst_2/application-output.json"
        )
        self.assertEqual({first, second}, {"acme_analyst", "acme_analyst_2"})

    def test_upsert_still_matches_on_key(self):
        self.assertIn(
            "Query the database for a page whose `Key` equals", self.command
        )


class DownstreamConsumersUseTheSubmittedSet(unittest.TestCase):
    """`/interview` and `/setup` must read the exact archived submitted files,
    not the mutable drafts, once `/outcome` records the submission."""

    def test_interview_prefers_submitted_documents(self):
        text = INTERVIEW.read_text(encoding="utf-8")
        self.assertIn("submitted_documents", text)
        self.assertIn("do not call the drafts submitted", text)

    def test_setup_reads_submitted_documents_for_calibration(self):
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn("submitted_documents", text)
        self.assertIn("do not treat `drafted_for_review` outputs as documents an employer saw", text)


if __name__ == "__main__":
    unittest.main()

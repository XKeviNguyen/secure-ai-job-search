"""Regression guards for the dependency-review CI capability gate.

The job used to probe the repository's dependency-graph SBOM endpoint with
curl and run actions/dependency-review-action when it returned HTTP 200. That
is wrong for private repositories: the SBOM endpoint can succeed while the
Dependency review action is still unsupported (missing GitHub Advanced
Security), so the action hard-failed with "Dependency review is not supported
on this repository" and re-running could never fix it.

This is a security-adjacent workflow, so the guards pin the policy rather than
a network behavior:

- the obsolete SBOM/curl probe is gone;
- the real action is still present, SHA-pinned, and strict
  (fail-on-severity: high);
- public repositories run it unconditionally;
- private repositories run it only with the DEPENDENCY_REVIEW_ENABLED opt-in,
  and otherwise get an explicit warning;
- continue-on-error / failure swallowing is never introduced.
"""
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

JOB_HEADER = "\n  dependency-review:\n"


def _job_block(text: str) -> str:
    """The dependency-review job, up to the next two-space-indented job key."""
    start = text.index(JOB_HEADER)
    rest = text[start + len(JOB_HEADER):]
    end = re.search(r"\n  [a-z][a-z0-9-]*:\n", rest)
    return rest[: end.start()] if end else rest


class DependencyReviewCapabilityGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.job = _job_block(cls.text)

    def test_obsolete_dependency_graph_probe_is_gone(self):
        for obsolete in (
            "dependency-graph/sbom",
            "Probe Dependency graph",
            "curl -s",
            "steps.graph.outputs",
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(
                    obsolete,
                    self.job,
                    "the SBOM/curl probe does not prove dependency-review support "
                    "and must not come back",
                )

    def test_real_action_remains_and_is_strict(self):
        self.assertIn("actions/dependency-review-action@", self.job)
        self.assertRegex(self.job, r"fail-on-severity:\s*high")

    def test_action_is_sha_pinned(self):
        for line in self.job.splitlines():
            stripped = line.strip()
            if stripped.startswith("- uses:") or stripped.startswith("uses:"):
                ref = stripped.split("uses:", 1)[1].strip()
                sha = ref.split("@", 1)[1].split()[0]
                self.assertRegex(
                    sha,
                    r"^[0-9a-f]{40}$",
                    f"dependency-review action must be SHA-pinned: {ref}",
                )

    def test_public_repositories_run_dependency_review(self):
        self.assertIn("github.event.repository.private == false", self.job)

    def test_private_repositories_require_the_opt_in_variable(self):
        self.assertIn("vars.DEPENDENCY_REVIEW_ENABLED == 'true'", self.job)

    def test_private_repositories_without_opt_in_get_a_warning(self):
        self.assertIn("github.event.repository.private == true", self.job)
        self.assertIn("vars.DEPENDENCY_REVIEW_ENABLED != 'true'", self.job)
        self.assertRegex(
            self.job,
            r"::warning\b",
            "a private repo without the opt-in must emit a clear warning, "
            "not silently pass",
        )
        self.assertIn("DEPENDENCY_REVIEW_ENABLED=true", self.job)

    def test_private_repo_warning_names_the_capability_requirement(self):
        self.assertRegex(self.job, r"(?i)Advanced Security")
        self.assertRegex(self.job, r"(?i)capability")

    def test_no_failure_swallowing_or_fake_success(self):
        # Inspect executable YAML/run lines only - the explanatory comment
        # legitimately says "no continue-on-error".
        code = "\n".join(
            line
            for line in self.job.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn(
            "continue-on-error:",
            code,
            "a high-severity finding must fail the job; swallowing it would "
            "turn the guard into decoration",
        )
        self.assertNotIn("|| true", code)
        self.assertNotIn("|| :", code)

    def test_only_runs_on_pull_requests(self):
        self.assertIn("github.event_name == 'pull_request'", self.job)

    def test_action_step_is_gated_by_the_capability_policy(self):
        """The action step itself carries the public-or-opt-in condition, not
        an always-true one, so a private repo without opt-in never runs it."""
        match = re.search(
            r"name:\s*Dependency review\s*\n\s+if:\s*(.+)", self.job
        )
        self.assertIsNotNone(match, "Dependency review action step not found")
        condition = match.group(1)
        self.assertIn("github.event.repository.private == false", condition)
        self.assertIn("vars.DEPENDENCY_REVIEW_ENABLED == 'true'", condition)


if __name__ == "__main__":
    unittest.main()

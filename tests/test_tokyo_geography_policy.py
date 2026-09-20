"""Guards for the configurable location/commute policy (candidate-neutral).

The framework ships no city and no commute threshold as a universal rule. The
policy is read from config/candidate-preferences.yaml, which /setup generates
from config/candidate-preferences.example.yaml. These tests pin that the specs
describe configurable behavior and never assume one candidate's city.
"""
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
EVALUATION = REPO / ".claude" / "skills" / "job-application-assistant" / "04-job-evaluation.md"
SEARCH = REPO / ".claude" / "skills" / "job-scraper" / "search-queries.md"
CONFIG_EXAMPLE = REPO / "config" / "candidate-preferences.example.yaml"


class ConfigurableGeographyPolicyGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evaluation = EVALUATION.read_text(encoding="utf-8")
        cls.search = SEARCH.read_text(encoding="utf-8")
        cls.config = CONFIG_EXAMPLE.read_text(encoding="utf-8")

    def test_policy_is_config_driven(self):
        for key in ("preferred_locations", "nearby_regions", "max_commute_minutes"):
            with self.subTest(key=key):
                self.assertIn(key, self.evaluation)
                self.assertIn(key, self.config)

    def test_commute_boundaries_map_to_pass_flag_fail(self):
        self.assertRegex(self.evaluation, r"at or below `max_commute_minutes` is \*\*PASS\*\*")
        self.assertRegex(self.evaluation, r"slightly above is \*\*FLAG\*\*")
        self.assertRegex(self.evaluation, r"clearly much longer is \*\*FAIL\*\*")
        self.assertRegex(self.evaluation, r"unknown workplace/station is \*\*FLAG\*\*")

    def test_nearby_regions_require_station_level_check(self):
        self.assertIn("actual workplace station/location", self.evaluation)
        self.assertIn("never pass an entire region", self.evaluation)
        self.assertIn("Do not approve a whole region", self.search)

    def test_no_city_is_a_universal_default(self):
        self.assertNotIn("Tokyo", self.evaluation)
        self.assertNotIn("Tokyo", self.search)

    def test_international_remote_roles_remain_in_scope(self):
        self.assertIn(
            "International remote roles that can engage the candidate from their location remain in scope",
            self.evaluation,
        )

    def test_example_config_is_a_clearly_generic_example(self):
        self.assertIn("max_commute_minutes: 60", self.config)
        self.assertIn("Example City", self.config)
        self.assertIn("example", self.config.lower())


if __name__ == "__main__":
    unittest.main()

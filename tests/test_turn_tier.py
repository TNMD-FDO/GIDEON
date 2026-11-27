"""Authority tiers and their relation to the controlling court set."""

import unittest

from test_courtmap import _generate

from gideon.host.courts import CourtMap
from gideon.turn import tier
from gideon.worker import caselaw


class AuthorityTiers(unittest.TestCase):
    def setUp(self) -> None:
        court_map = _generate()
        assert isinstance(court_map, CourtMap)
        self.court_map = court_map
        self.jurisdiction = tier.Jurisdiction(
            court_map.ids_at_level("scotus"), ("ca11",), ("alnd",), court_map.appellate_courts("AL"),
        )

    def test_only_the_home_circuit_tier_depends_on_precedential_status(self) -> None:
        for court, expected in (
            ("scotus", "scotus"), ("alnd", "district_home"), ("fx-sup-al", "state_home"),
            ("fx-app-al", "state_home"), ("ca1", "persuasive"),
        ):
            for status in tier.PRECEDENTIAL_VALUES:
                with self.subTest(court=court, status=status):
                    self.assertEqual(tier.authority_tier("caselaw", court, status, self.jurisdiction), expected)
        for status, expected in (
            ("published", "circuit_published"),
            ("unpublished", "circuit_unpublished"),
            ("unknown", "circuit_unpublished"),
        ):
            with self.subTest(status=status):
                self.assertEqual(tier.authority_tier("caselaw", "ca11", status, self.jurisdiction), expected)

    def test_authorities_and_matter_get_their_source_tier(self) -> None:
        self.assertEqual(tier.authority_tier("authorities", "", "unknown", self.jurisdiction), "authority")
        self.assertEqual(tier.authority_tier("matter", "", "unknown", self.jurisdiction), "matter")

    def test_caselaw_is_persuasive_exactly_outside_the_controlling_set(self) -> None:
        controlling = self.court_map.controlling_courts("ca11", ("alnd",), ("fx-sup-al",))
        self.assertEqual(self.jurisdiction.controlling_courts, frozenset(controlling))
        for court in self.court_map.courts:
            for status in tier.PRECEDENTIAL_VALUES:
                with self.subTest(court=court, status=status):
                    label = tier.authority_tier("caselaw", court, status, self.jurisdiction)
                    self.assertEqual(label == "persuasive", court not in controlling)

    def test_precedential_vocabulary_matches_worker(self) -> None:
        self.assertEqual(tier.PRECEDENTIAL_VALUES, caselaw.PRECEDENTIAL_VALUES)

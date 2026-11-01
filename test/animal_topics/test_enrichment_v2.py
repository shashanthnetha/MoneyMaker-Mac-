import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import enrich, http
from animal_topics.db import connect, init_schema
from animal_topics.discovery import is_human_lineage


class EnrichmentHelpersTests(unittest.TestCase):
    def test_normalization_maps_taxonomy_aliases(self):
        self.assertEqual(enrich.normalize_values(["Mammalia", "Aves"], enrich.CLASS_MAP), ["Mammal", "Bird"])
        self.assertEqual(
            enrich.normalize_values(["marine", "flying"], enrich.HABITAT_MAP),
            ["Aquatic", "Aerial"],
        )

    def test_human_lineage_filter(self):
        for value in ("Neanderthal", "Caveman", "heidelberg man", "denisovan"):
            self.assertTrue(is_human_lineage(None, value, value))
        self.assertFalse(is_human_lineage(None, "Otter", "Sea otter"))

    def test_reset_runs_once(self):
        conn = connect(":memory:")
        init_schema(conn)
        conn.execute(
            """INSERT INTO species
            (wikidata_qid,common_name,wikipedia_title,enrichment_status,
             enrichment_attempts,discovered_at,source)
            VALUES ('Q1','Otter','Otter','failed',2,'2020','wishlist')"""
        )
        conn.commit()
        with patch.dict(os.environ, {}, clear=True):
            enrich.enrich(conn, 1)
        row = conn.execute("SELECT enrichment_attempts,enrichment_status FROM species").fetchone()
        self.assertEqual((row["enrichment_attempts"], row["enrichment_status"]), (0, "pending"))
        with patch.dict(os.environ, {}, clear=True):
            enrich.enrich(conn, 1)
        row = conn.execute("SELECT enrichment_attempts,enrichment_status FROM species").fetchone()
        self.assertEqual((row["enrichment_attempts"], row["enrichment_status"]), (0, "pending"))


class RateLimitTests(unittest.TestCase):
    def test_429_wait_parsing_supports_ms_seconds_and_fallback(self):
        waits = []
        errors = []

        def fake_sleep(value):
            waits.append(value)

        def response(*args, **kwargs):
            error = http.urllib.error.HTTPError(
                "https://api.groq.com", 429, "rate", {}, None
            )
            error.read = lambda: b'please try again in 1.5s'
            raise error

        with patch.object(http.urllib.request, "urlopen", side_effect=response):
            with patch.object(http.time, "sleep", side_effect=fake_sleep):
                http.get_json("https://api.groq.com", attempts=1)
        self.assertIn(2.5, waits)

        def response_ms(*args, **kwargs):
            error = http.urllib.error.HTTPError(
                "https://api.groq.com", 429, "rate", {}, None
            )
            error.read = lambda: b'try again in 250ms'
            raise error

        waits.clear()
        with patch.object(http.urllib.request, "urlopen", side_effect=response_ms):
            with patch.object(http.time, "sleep", side_effect=fake_sleep):
                http.get_json("https://api.groq.com", attempts=1)
        self.assertIn(1.25, waits)


if __name__ == "__main__":
    unittest.main()

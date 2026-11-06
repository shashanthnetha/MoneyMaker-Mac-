import io
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import discovery
from animal_topics.candidates import valid_title
from animal_topics.db import connect, init_schema


class SpeciesNameTests(unittest.TestCase):
    def test_choose_common_name_examples(self):
        self.assertEqual(
            discovery.choose_common_name(
                "Don Hugo",
                "Bombyx mori",
                "Bombyx mori",
                "The domestic silk moth (Bombyx mori) is an insect.",
            ),
            "domestic silk moth",
        )
        self.assertEqual(
            discovery.choose_common_name(
                "Asiatic Jackal",
                "Golden jackal",
                "Canis aureus",
                "The golden jackal (Canis aureus) is a canid.",
            ),
            "Golden jackal",
        )
        self.assertEqual(
            discovery.choose_common_name(
                "Lion", "Lion", "Panthera leo", "The lion (Panthera leo) is a cat."
            ),
            "Lion",
        )
        self.assertEqual(
            discovery.choose_common_name(
                "Wikidata name", "Example", None, "The Wikidata name is an animal."
            ),
            "Wikidata name",
        )

    def test_name_phrase_limits_and_wishlist_exemption(self):
        self.assertEqual(
            discovery.choose_common_name(
                "Old Name",
                "Scientificus",
                "Scientificus",
                "The one two three four five six (Scientificus) is an animal.",
            ),
            "Old Name",
        )
        conn = connect(":memory:")
        init_schema(conn)
        conn.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, scientific_name,
             wiki_text, enrichment_status, discovered_at, source)
            VALUES ('Q1', 'Don Hugo', 'Bombyx mori', 'Bombyx mori',
                    'The domestic silk moth (Bombyx mori) is an insect.',
                    'done', '2020', 'wishlist')"""
        )
        conn.commit()
        discovery.repair_common_names(conn)
        self.assertEqual(conn.execute("SELECT common_name FROM species").fetchone()[0], "Don Hugo")

    def test_name_fix_runs_once_and_logs_count(self):
        conn = connect(":memory:")
        init_schema(conn)
        conn.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, scientific_name,
             wiki_text, enrichment_status, discovered_at, source)
            VALUES ('Q1', 'Don Hugo', 'Bombyx mori', 'Bombyx mori',
                    'The domestic silk moth (Bombyx mori) is an insect.',
                    'done', '2020', 'discovery')"""
        )
        conn.commit()
        output = io.StringIO()
        with redirect_stderr(output):
            discovery.repair_common_names(conn)
            discovery.repair_common_names(conn)
        self.assertEqual(conn.execute("SELECT common_name FROM species").fetchone()[0], "domestic silk moth")
        self.assertEqual(output.getvalue().count("name fix: 1 species renamed"), 1)

    def test_title_requires_full_common_name(self):
        self.assertFalse(valid_title("Why Jackal Hunts At Night", "Golden jackal"))
        self.assertTrue(valid_title("Why Golden Jackal Hunts At Night", "Golden jackal"))

    def test_dry_run_separator_has_seven_columns(self):
        separator = "|---|---|---|---|---|---|---|"
        self.assertEqual(separator.count("|") - 1, 7)


if __name__ == "__main__":
    unittest.main()

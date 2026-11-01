import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import candidates, discovery, enrich
from animal_topics.db import connect, init_schema


class TitleQualityTests(unittest.TestCase):
    def test_taxonomy_filter_and_interest_selection(self):
        self.assertTrue(candidates.is_taxonomy_fact(
            "The common cuckoo belongs to the bird order Cuculiformes"
        ))
        self.assertTrue(candidates.is_taxonomy_fact(
            "The common blackbird is a species of true thrush"
        ))
        self.assertFalse(candidates.is_taxonomy_fact(
            "Male Komodo dragons can grow up to 3 m long"
        ))
        options = [
            {"title": "Low", "interest": 4},
            {"title": "High", "interest": 9},
            {"title": "Tie", "interest": 9},
        ]
        self.assertEqual(candidates.choose_by_interest(options)["title"], "High")
        self.assertEqual(
            candidates.choose_by_interest([{"title": "Missing"}, {"title": "Zero", "interest": "8"}])["title"],
            "Missing",
        )

    def test_section_extraction_keeps_matching_sections_and_caps_output(self):
        article = (
            "Lead " + "x" * 1200 + "\n"
            "== Behaviour ==\n" + "b" * 1200 + "\n"
            "== Taxonomy ==\n" + "t" * 1200 + "\n"
            "== Diet ==\n" + "d" * 1200 + "\n"
            "== Habitat ==\n" + "h" * 1200 + "\n"
            "== Reproduction ==\n" + "r" * 1200
        )
        text = enrich.build_wiki_text(article)
        self.assertLessEqual(len(text), 5500)
        self.assertIn("== Behaviour ==", text)
        self.assertIn("== Diet ==", text)
        self.assertNotIn("== Taxonomy ==", text)

        short_sections = "Lead\n== Taxonomy ==\nclassification only"
        self.assertEqual(enrich.build_wiki_text(short_sections), short_sections[:5500])

    def test_text_refresh_runs_once_keeps_failed_text_and_skips_groq(self):
        conn = connect(":memory:")
        init_schema(conn)
        for qid, title, text in (
            ("Q1", "Otter", "old otter text"),
            ("Q2", "Badger", "old badger text"),
        ):
            conn.execute(
                """INSERT INTO species
                (wikidata_qid, common_name, wikipedia_title, wiki_text,
                 enrichment_status, discovered_at, source)
                VALUES (?, ?, ?, ?, 'done', '2020', 'discovery')""",
                (qid, title, title, text),
            )
        conn.commit()
        good = {"query": {"pages": {"1": {"extract": "Lead " + "x" * 1800}}}}
        with patch.dict(os.environ, {}, clear=True), patch.object(
            enrich, "get_json", side_effect=[good, None]
        ), patch.object(enrich, "call") as groq, patch.object(enrich.time, "sleep"):
            enrich.enrich(conn, 0)
            enrich.enrich(conn, 0)
        groq.assert_not_called()
        rows = conn.execute(
            "SELECT common_name, wiki_text FROM species ORDER BY id"
        ).fetchall()
        self.assertNotEqual(rows[0]["wiki_text"], "old otter text")
        self.assertEqual(rows[1]["wiki_text"], "old badger text")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM meta WHERE key='text_refresh_v1'").fetchone()[0],
            1,
        )

    def test_discovery_stops_at_two_thousand_species(self):
        conn = connect(":memory:")
        init_schema(conn)
        for index in range(2000):
            conn.execute(
                """INSERT INTO species
                (wikidata_qid, common_name, wikipedia_title, discovered_at, source)
                VALUES (?, ?, ?, '2020', 'discovery')""",
                (f"Q{index}", f"Animal {index}", f"Animal {index}"),
            )
        conn.commit()
        output = io.StringIO()
        with patch.object(discovery, "validate_labels") as labels, patch.object(
            discovery, "apply"
        ) as wishlist, redirect_stderr(output):
            discovery.discover(conn, 10, fetch=lambda *args, **kwargs: self.fail("network"))
        labels.assert_not_called()
        wishlist.assert_not_called()
        self.assertIn("species pool already has 2000", output.getvalue())


if __name__ == "__main__":
    unittest.main()

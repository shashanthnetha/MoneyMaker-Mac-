import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import enrich
from animal_topics.db import connect, init_schema
from animal_topics.selection import select_species


class PexelsBudgetTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        init_schema(self.conn)

    def add_species(
        self,
        name,
        *,
        status="active",
        enrichment_status="done",
        footage_score=None,
        source="discovery",
        popularity=None,
        keywords=None,
        attempts=0,
        disabled_reason=None,
        is_extinct=0,
    ):
        self.conn.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, is_extinct,
             popularity, visual_keywords, footage_score, enrichment_status,
             enrichment_attempts, status, disabled_reason, discovered_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2020', ?)""",
            (
                name,
                name,
                name,
                is_extinct,
                popularity,
                json.dumps(keywords) if keywords is not None else None,
                footage_score,
                enrichment_status,
                attempts,
                status,
                disabled_reason,
                source,
            ),
        )
        self.conn.commit()

    @staticmethod
    def response(total, remaining=100):
        return {
            "ok": True,
            "throttled": False,
            "payload": {"total_results": total},
            "remaining": remaining,
            "attempts": 1,
        }

    def run_footage(self, responses, *, batch=0):
        calls = []

        def fake_search(api_key, query, max_attempts=4):
            calls.append(query)
            result = responses.pop(0)
            return result

        env = {"PEXELS_API_KEY": "pexels"}
        with patch.dict(os.environ, env, clear=False), patch.object(
            enrich, "_pexels_search", side_effect=fake_search
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                enrich.enrich(self.conn, batch, sleeper=lambda _: None)
        return calls, stderr.getvalue()

    def test_stage_one_never_calls_pexels(self):
        self.add_species("Otter", enrichment_status="pending", keywords=None)
        wiki = {"query": {"pages": {"1": {"extract": "x" * 500}}}}
        metadata = {
            "result": {
                "classes": ["Mammal"],
                "habitats": ["Terrestrial"],
                "visual_keywords": ["otter"],
            },
            "error": None,
            "status": 200,
        }
        with patch.dict(
            os.environ,
            {"GROQ_API_KEY": "groq"},
            clear=False,
        ), patch.object(enrich, "get_json", return_value=wiki), patch.object(
            enrich, "call", return_value=metadata
        ), patch.object(enrich, "_pexels_search") as pexels:
            with patch.dict(os.environ, {"PEXELS_API_KEY": ""}, clear=False):
                enrich.enrich(self.conn, 1)
        pexels.assert_not_called()
        row = self.conn.execute(
            "SELECT enrichment_status, footage_score FROM species"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("done", None))

    def test_429_leaves_current_species_and_warns(self):
        self.add_species("Otter", keywords=["otter", "river"])
        calls, stderr = self.run_footage(
            [{"ok": False, "throttled": True, "payload": None, "remaining": None, "attempts": 1}]
        )
        row = self.conn.execute(
            "SELECT status, footage_score, disabled_reason FROM species"
        ).fetchone()
        self.assertEqual(calls, ["Otter"])
        self.assertEqual(tuple(row), ("active", None, None))
        self.assertIn("::warning::Pexels rate limit reached", stderr)

    def test_failed_species_is_untouched_and_next_species_runs(self):
        self.add_species("Otter", keywords=["otter", "river"])
        self.add_species("Badger", keywords=["badger", "forest"])
        calls, _ = self.run_footage(
            [
                {"ok": False, "throttled": False, "payload": None, "remaining": None, "attempts": 1},
                self.response(20),
            ]
        )
        rows = self.conn.execute(
            "SELECT common_name, footage_score, status FROM species ORDER BY id"
        ).fetchall()
        self.assertEqual(calls, ["Otter", "Badger"])
        self.assertEqual((rows[0][1], rows[0][2]), (None, "active"))
        self.assertEqual((rows[1][1], rows[1][2]), (20, "active"))

    def test_each_species_uses_at_most_two_queries_and_stops_on_good_first_result(self):
        self.add_species("Otter", keywords=["otter", "river", "water"])
        calls, _ = self.run_footage([self.response(20)])
        self.assertEqual(calls, ["Otter"])
        self.assertEqual(
            self.conn.execute("SELECT footage_score FROM species").fetchone()[0], 20
        )

    def test_low_success_score_disables_but_failure_does_not(self):
        self.add_species("Otter", keywords=["otter", "river"])
        calls, _ = self.run_footage([self.response(2), self.response(7)])
        row = self.conn.execute(
            "SELECT footage_score, status, disabled_reason FROM species"
        ).fetchone()
        self.assertEqual(calls, ["Otter", "otter"])
        self.assertEqual(tuple(row), (7, "disabled", "low_footage"))

    def test_low_remaining_finishes_species_then_stops(self):
        self.add_species("Otter", keywords=["otter", "river"])
        self.add_species("Badger", keywords=["badger", "forest"])
        calls, stderr = self.run_footage(
            [self.response(2, 20), self.response(7, 20), self.response(20, 100)]
        )
        self.assertEqual(calls, ["Otter", "otter"])
        self.assertIn("remaining species will be checked", stderr)
        self.assertEqual(
            self.conn.execute("SELECT footage_score FROM species WHERE common_name='Otter'").fetchone()[0],
            7,
        )

    def test_wishlist_then_popularity_order(self):
        self.add_species("DiscoveryHigh", keywords=["high"], popularity=100)
        self.add_species("WishlistLow", keywords=["wish"], source="wishlist", popularity=1)
        self.add_species("DiscoveryLow", keywords=["low"], popularity=2)
        calls, _ = self.run_footage([self.response(20), self.response(20), self.response(20)])
        self.assertEqual(calls, ["WishlistLow", "DiscoveryHigh", "DiscoveryLow"])

    def test_request_budget_stops_after_100_attempts(self):
        for index in range(51):
            self.add_species(f"Species{index}", keywords=[f"keyword{index}"])
        responses = [self.response(0) for _ in range(100)]
        calls, stderr = self.run_footage(responses)
        self.assertEqual(len(calls), 100)
        self.assertIn("(50 checked, 1 pending)", stderr)

    def test_reset_v3_runs_once_and_selection_allows_null_footage(self):
        self.add_species(
            "ResetMe",
            status="disabled",
            footage_score=None,
            disabled_reason="low_footage",
            keywords=["reset"],
        )
        self.add_species("Eligible", footage_score=20, keywords=["eligible"])
        self.conn.execute(
            "UPDATE species SET wiki_text='Eligible is an animal.' WHERE common_name='Eligible'"
        )
        self.conn.commit()
        with patch.dict(os.environ, {}, clear=True):
            enrich.enrich(self.conn, 0)
        row = self.conn.execute(
            "SELECT status, disabled_reason, footage_score FROM species WHERE common_name='ResetMe'"
        ).fetchone()
        self.assertEqual(tuple(row), ("active", None, None))
        self.assertIn(
            select_species(self.conn)["common_name"], {"ResetMe", "Eligible"}
        )
        self.conn.execute(
            "UPDATE species SET disabled_reason='manual' WHERE common_name='ResetMe'"
        )
        self.conn.commit()
        with patch.dict(os.environ, {}, clear=True):
            enrich.enrich(self.conn, 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT disabled_reason FROM species WHERE common_name='ResetMe'"
            ).fetchone()[0],
            "manual",
        )

    def test_selection_allows_unchecked_footage(self):
        self.add_species("Unchecked", footage_score=None)
        self.conn.execute(
            "UPDATE species SET wiki_text='text' WHERE common_name='Unchecked'"
        )
        self.conn.commit()
        self.assertEqual(select_species(self.conn)["common_name"], "Unchecked")

    def test_selection_excludes_score_below_threshold(self):
        self.add_species("LowScore", footage_score=14)
        self.conn.execute(
            "UPDATE species SET wiki_text='text' WHERE common_name='LowScore'"
        )
        self.conn.commit()
        self.assertIsNone(select_species(self.conn))

    def test_selection_allows_score_at_threshold(self):
        self.add_species("GoodScore", footage_score=15)
        self.conn.execute(
            "UPDATE species SET wiki_text='text' WHERE common_name='GoodScore'"
        )
        self.conn.commit()
        self.assertEqual(select_species(self.conn)["common_name"], "GoodScore")

    def test_selection_excludes_disabled_unchecked_species(self):
        self.add_species("DisabledUnchecked", status="disabled", footage_score=None)
        self.conn.execute(
            "UPDATE species SET wiki_text='text' WHERE common_name='DisabledUnchecked'"
        )
        self.conn.commit()
        self.assertIsNone(select_species(self.conn))


if __name__ == "__main__":
    unittest.main()

import unittest
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from animal_topics import discovery, http, wishlist
from animal_topics.db import connect, init_schema


def binding(qid, name=None, sci=None, links="10"):
    row = {
        "taxon": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "links": {"value": links},
    }
    if name:
        row["name"] = {"value": name}
    if sci:
        row["sci"] = {"value": sci}
    return row


class DiscoveryQleverTests(unittest.TestCase):
    def test_queries_use_qlever_prefixes_and_no_schema(self):
        query = discovery.ranked_query("living", 20)
        self.assertTrue(query.startswith("PREFIX wd:"))
        self.assertNotIn("schema:", query)
        self.assertIn("FILTER(?links >= 20)", query)
        self.assertIn("LIMIT 200 OFFSET 0", query)

    def test_page_rows_keep_first_duplicate_qid(self):
        responses = [
            {"results": {"bindings": [
                binding("Q1"),
                binding("Q1"),
                binding("Q2"),
            ]}},
        ]

        rows = discovery._page_rows("living", 20, 0, lambda *args, **kwargs: responses.pop(0))
        self.assertEqual([row["qid"] for row in rows], ["Q1", "Q2"])
        self.assertEqual(rows[0]["links"], "10")

    def test_step_b_drops_plants_and_living_extinct(self):
        data = {"results": {"bindings": [
            {"taxon": {"value": "http://www.wikidata.org/entity/Q1"}, "ext": {"value": "false"},
             "name": {"value": "Animal"}, "sci": {"value": "Animalus one"}},
            {"taxon": {"value": "http://www.wikidata.org/entity/Q2"}, "ext": {"value": "true"},
             "name": {"value": "Fossil"}, "sci": {"value": "Animalus two"}},
        ]}}
        rows = [{"qid": "Q1"}, {"qid": "Q2"}, {"qid": "Q3"}]
        fetch = lambda *args, **kwargs: data
        living = discovery._animal_rows(rows, "living", fetch)
        extinct = discovery._animal_rows(rows, "extinct", fetch)
        self.assertEqual([r["qid"] for r in living], ["Q1"])
        self.assertEqual([r["qid"] for r in extinct], ["Q1", "Q2"])
        self.assertEqual(living[0]["name"], "Animal")
        self.assertEqual(living[0]["sci"], "Animalus one")

    def test_step_b_query_requests_names(self):
        query = discovery.animal_filter_query(["Q1"])
        self.assertIn("SELECT ?taxon ?name ?sci ?ext", query)
        self.assertIn("OPTIONAL", query)

    def test_short_page_advances_ladder_and_full_offset(self):
        conn = connect(":memory:")
        init_schema(conn)
        row = {"qid": "Q1", "links": "20", "_index": 0, "extinct": False}
        with patch.object(discovery, "validate_labels"), patch.object(
            discovery, "apply"
        ), patch.object(discovery, "_page_rows", return_value=[row]), patch.object(
            discovery, "_animal_rows", return_value=[row]
        ), patch.object(
            discovery, "fetch_titles", return_value={"Q1": "Animal"}
        ), patch.object(
            discovery, "_insert", return_value=None
        ):
            discovery.discover(conn, 1, fetch=lambda *args, **kwargs: {})
        self.assertEqual(
            conn.execute(
                "SELECT value FROM meta WHERE key='living_min_sitelinks'"
            ).fetchone()["value"],
            "10",
        )
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='living_offset'").fetchone()[
                "value"
            ],
            "200",
        )


class HttpLoggingTests(unittest.TestCase):
    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok": true}'

    def test_successful_sparql_logs_elapsed(self):
        output = StringIO()
        with patch.object(http.urllib.request, "urlopen", return_value=self.Response()):
            with redirect_stderr(output):
                http.get_json(
                    "https://qlever.cs.uni-freiburg.de/api/wikidata",
                    attempts=1,
                )
        self.assertIn("SPARQL request succeeded", output.getvalue())
        self.assertIn("elapsed", output.getvalue())

    def test_failed_http_logs_host_exception_and_elapsed(self):
        output = StringIO()
        with patch.object(
            http.urllib.request,
            "urlopen",
            side_effect=http.urllib.error.URLError("offline"),
        ):
            with redirect_stderr(output):
                http.get_json("https://qlever.cs.uni-freiburg.de/api/wikidata", attempts=1)
        message = output.getvalue()
        self.assertIn("qlever.cs.uni-freiburg.de", message)
        self.assertIn("URLError", message)
        self.assertIn("offline", message)
        self.assertIn("elapsed", message)

    def test_title_batches_read_enwiki_titles(self):
        calls = []

        def fetch(url, params=None, **kwargs):
            calls.append(params)
            return {"entities": {
                "Q1": {"sitelinks": {"enwiki": {"title": "Animal one"}}},
                "Q2": {"sitelinks": {}},
            }}

        self.assertEqual(discovery.fetch_titles(["Q1", "Q2"], fetch), {"Q1": "Animal one"})
        self.assertEqual(calls[0]["sitefilter"], "enwiki")

    def test_wishlist_query_has_no_schema_and_uses_qlever(self):
        query = wishlist.animal_query("Q1")
        self.assertTrue(query.startswith("PREFIX wd:"))
        self.assertNotIn("schema:", query)
        self.assertIn("VALUES ?taxon { wd:Q1 }", query)

    def test_label_mismatch_warns_and_continues(self):
        data = {"entities": {"Q7432": {"labels": {"en": {"value": "wrong"}}}}}
        output = StringIO()
        with redirect_stderr(output):
            discovery.validate_labels(lambda *args, **kwargs: data)
        self.assertIn("label check warning: Q7432", output.getvalue())

    def test_q729_animal_label_is_accepted(self):
        data = {"entities": {"Q729": {"labels": {"en": {"value": "animal"}}}}}
        output = StringIO()
        with redirect_stderr(output):
            discovery.validate_labels(lambda *args, **kwargs: data)
        self.assertNotIn("Q729", output.getvalue())


if __name__ == "__main__":
    unittest.main()

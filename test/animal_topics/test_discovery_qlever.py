import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from animal_topics import discovery, wishlist


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
        query = discovery.ranked_query("living")
        self.assertTrue(query.startswith("PREFIX wd:"))
        self.assertNotIn("schema:", query)
        self.assertIn("LIMIT 200 OFFSET 0", query)

    def test_page_rows_keep_first_duplicate_qid(self):
        responses = [
            {"results": {"bindings": [
                binding("Q1", "First"),
                binding("Q1", "Second"),
                binding("Q2", "Other"),
            ]}},
        ]

        rows = discovery._page_rows("living", 0, lambda *args, **kwargs: responses.pop(0))
        self.assertEqual([row["qid"] for row in rows], ["Q1", "Q2"])
        self.assertEqual(rows[0]["name"], "First")

    def test_step_b_drops_plants_and_living_extinct(self):
        data = {"results": {"bindings": [
            {"taxon": {"value": "http://www.wikidata.org/entity/Q1"}, "ext": {"value": "false"}},
            {"taxon": {"value": "http://www.wikidata.org/entity/Q2"}, "ext": {"value": "true"}},
        ]}}
        rows = [{"qid": "Q1"}, {"qid": "Q2"}, {"qid": "Q3"}]
        fetch = lambda *args, **kwargs: data
        self.assertEqual([r["qid"] for r in discovery._animal_rows(rows, "living", fetch)], ["Q1"])
        self.assertEqual([r["qid"] for r in discovery._animal_rows(rows, "extinct", fetch)], ["Q1", "Q2"])

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

    def test_label_mismatch_aborts(self):
        data = {"entities": {"Q7432": {"labels": {"en": {"value": "wrong"}}}}}
        with self.assertRaises(RuntimeError):
            discovery.validate_labels(lambda *args, **kwargs: data)


if __name__ == "__main__":
    unittest.main()

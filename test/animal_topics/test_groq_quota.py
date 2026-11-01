import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import cli, enrich, groq, http, pipeline
from animal_topics.db import connect, init_schema


class GroqQuotaTests(unittest.TestCase):
    def setUp(self):
        groq._QUOTA_EXHAUSTED = False
        groq._ENRICH_MODEL_FALLBACK = False
        groq._MODEL_FALLBACK_LOGGED = False
        pipeline._QUOTA_LOGGED = False

    def test_tpd_and_long_wait_quota_do_not_retry_or_sleep(self):
        for body in (b"tokens per day (TPD): Limit 200000", b"try again in 120s"):
            error = http.urllib.error.HTTPError("https://api.groq.com", 429, "rate", {}, None)
            error.read = lambda body=body: body
            with patch.object(http.urllib.request, "urlopen", side_effect=error), patch.object(
                http.time, "sleep"
            ) as sleep:
                result = http.post_json("https://api.groq.com", {}, attempts=1)
            self.assertTrue(result["_quota_exhausted"])
            sleep.assert_not_called()

    def test_short_wait_retries(self):
        calls = []

        def response(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                error = http.urllib.error.HTTPError("https://api.groq.com", 429, "rate", {}, None)
                error.read = lambda: b"try again in 5s"
                raise error
            class Good:
                status = 200
                headers = {}
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return b"{}"
            return Good()

        with patch.object(http.urllib.request, "urlopen", side_effect=response), patch.object(
            http.time, "sleep"
        ) as sleep:
            result = http.post_json("https://api.groq.com", {}, attempts=1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(sleep.called)
        self.assertEqual(result.get("_headers"), {})

    def test_models_and_reasoning_fields(self):
        responses = [{"choices": [{"message": {"content": "{}"}}]}] * 3
        with patch.object(groq, "post_json", side_effect=responses) as post, patch.dict(
            os.environ,
            {"GROQ_API_KEY": "key", "GROQ_ENRICH_MODEL": "llama-test", "GROQ_MODEL": "openai/gpt-oss-120b"},
            clear=False,
        ):
            groq.call("x", return_metadata=True, model=os.environ["GROQ_ENRICH_MODEL"])
            groq.call("x", return_metadata=True, model=os.environ["GROQ_MODEL"])
        enrich_body = post.call_args_list[0].args[1]
        candidate_body = post.call_args_list[1].args[1]
        self.assertEqual(enrich_body["model"], "llama-test")
        self.assertEqual(enrich_body["max_tokens"], 600)
        self.assertNotIn("reasoning_effort", enrich_body)
        self.assertEqual(candidate_body["model"], "openai/gpt-oss-120b")
        self.assertEqual(candidate_body["max_tokens"], 1500)
        self.assertEqual(candidate_body["reasoning_effort"], "low")

    def test_enrichment_model_404_falls_back(self):
        error = {"_http_error": 404, "_raw_body": '{"error":"model not found"}'}
        good = {"choices": [{"message": {"content": "{}"}}]}
        with patch.object(groq, "post_json", side_effect=[error, good]) as post, patch.dict(
            os.environ,
            {"GROQ_API_KEY": "key", "GROQ_ENRICH_MODEL": "bad-model", "GROQ_MODEL": "good-model"},
            clear=False,
        ):
            result = groq.call("x", return_metadata=True, model="bad-model")
        self.assertEqual(result["result"], {})
        self.assertEqual(post.call_args_list[1].args[1]["model"], "good-model")

    def test_enrich_quota_warning_preserves_attempts(self):
        conn = connect(":memory:")
        init_schema(conn)
        conn.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, discovered_at, source)
            VALUES ('Q1', 'Otter', 'Otter', '2020', 'discovery')"""
        )
        conn.commit()
        metadata = {"error": "quota_exhausted", "status": 429, "result": None}
        with patch.dict(os.environ, {"GROQ_API_KEY": "key", "PEXELS_API_KEY": ""}, clear=False), patch.object(
            enrich, "get_json", return_value={"query": {"pages": {"1": {"extract": "x" * 500}}}}
        ), patch.object(enrich, "call", return_value=metadata), redirect_stderr(io.StringIO()) as err:
            enrich.enrich(conn, 1, sleeper=lambda _: None)
        row = conn.execute("SELECT enrichment_attempts, enrichment_status FROM species").fetchone()
        self.assertEqual(tuple(row), (0, "pending"))
        self.assertIn("Groq daily token quota exhausted", err.getvalue())

    def test_pipeline_quota_uses_fallback_without_judge(self):
        conn = connect(":memory:")
        init_schema(conn)
        conn.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, wiki_text,
             enrichment_status, footage_score, discovered_at, source)
            VALUES ('Q1', 'Otter', 'Otter', 'text', 'done', 20, '2020', 'discovery')"""
        )
        conn.commit()
        with patch.object(pipeline, "call", return_value={"error": "quota_exhausted"}), patch.object(
            pipeline, "survivors", side_effect=AssertionError("judge should be skipped")
        ), patch.object(pipeline, "select_species", return_value=conn.execute("SELECT * FROM species").fetchone()):
            title = pipeline.pick_for_connection(conn, {})
        self.assertIn("Surprising facts about", title)

    def test_prompt_limits_stored_text(self):
        row = {"common_name": "Otter", "is_extinct": 0}
        prompt = enrich._prompt(row, "x" * 2000)
        self.assertLessEqual(prompt.split("SOURCE TEXT:\n", 1)[1].split("\n\nAllowed", 1)[0].__len__(), 900)

    def test_dry_run_streams_rows_and_reports_source(self):
        db = sqlite3.connect(":memory:")
        init_schema(db)
        db.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, discovered_at, source)
            VALUES ('Q1', 'Otter', 'Otter', '2020', 'discovery')"""
        )
        db.commit()
        output = io.StringIO()
        calls = []

        def fake_pick(memory, env):
            index = len(calls)
            calls.append(index)
            sid = memory.execute("SELECT id FROM species").fetchone()[0]
            now = f"2020-01-0{index + 1}"
            fact_key = "fact-key" if index == 0 else "template-1"
            with memory:
                memory.execute("INSERT INTO facts(species_id,fact_text,fact_key,created_at) VALUES(?,?,?,?)", (sid, "fact", fact_key, now))
                fid = memory.execute("SELECT last_insert_rowid()").fetchone()[0]
                memory.execute("INSERT INTO topics(species_id,fact_id,title,title_norm,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (sid, fid, f"Title {index}", f"title-{index}", "reserved", now, now))

        with patch.object(cli, "connect", return_value=db), patch.object(
            cli, "pick_for_connection", side_effect=fake_pick
        ), redirect_stdout(output):
            cli.main(["dry-run", "--count", "2"])
        lines = output.getvalue().splitlines()
        self.assertIn("Source", lines[0])
        self.assertIn("AI", lines[2])
        self.assertIn("fallback", lines[3])


if __name__ == "__main__":
    unittest.main()

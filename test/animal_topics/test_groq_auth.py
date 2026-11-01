import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import dedupe, enrich, groq, http, pipeline
from animal_topics.db import connect, init_schema


class _Handler(BaseHTTPRequestHandler):
    status = 200
    statuses = []
    requests = []
    responses = []

    def do_POST(self):
        self.__class__.requests.append(dict(self.headers))
        body = self.__class__.responses.pop(0) if self.__class__.responses else {
            "candidates": []
        }
        status = self.__class__.statuses.pop(0) if self.__class__.statuses else self.__class__.status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_):
        pass


class GroqAuthTests(unittest.TestCase):
    def setUp(self):
        _Handler.requests = []
        _Handler.responses = []
        _Handler.statuses = []
        _Handler.status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        groq._AUTH_DIAGNOSTIC_LOGGED = False

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()

    @staticmethod
    def _success(value):
        return {
            "choices": [
                {
                    "message": {"content": json.dumps(value)},
                    "finish_reason": "stop",
                }
            ]
        }

    def test_shared_call_path_sends_bearer_header_and_retries(self):
        _Handler.responses = [
            {"error": "try again in 1ms"},
            self._success({"candidates": []}),
        ]
        _Handler.statuses = [429, 200]
        with patch.object(groq, "GROQ_URL", self.url), patch.dict(
            "os.environ", {"GROQ_API_KEY": "  gsk_dummy_key \n"}, clear=False
        ), patch.object(http.time, "sleep"):
            self.assertEqual(groq.call("prompt"), {"candidates": []})
        self.assertEqual(len(_Handler.requests), 2)
        self.assertTrue(
            all(request["Authorization"] == "Bearer gsk_dummy_key"
                for request in _Handler.requests)
        )

    def test_auth_failures_are_not_retried_or_slept_and_diagnostic_is_safe(self):
        for status in (401, 403):
            _Handler.status = status
            _Handler.statuses = [status]
            _Handler.responses = [{"error": "invalid gsk_secret_value"}]
            stderr = io.StringIO()
            with patch.object(groq, "GROQ_URL", self.url), patch.dict(
                "os.environ", {"GROQ_API_KEY": "  gsk_secret_value  "}, clear=False
            ), patch.object(http.time, "sleep") as sleep, redirect_stderr(stderr):
                result = groq.call("prompt", return_metadata=True)
            self.assertEqual(result["error"], "auth_error")
            self.assertEqual(result["status"], status)
            self.assertEqual(len(_Handler.requests), 1)
            sleep.assert_not_called()
            diagnostic = [
                line for line in stderr.getvalue().splitlines()
                if line.startswith("Groq auth diagnostics:")
            ][0]
            self.assertNotIn("gsk_secret_value", diagnostic)
            _Handler.requests = []
            groq._AUTH_DIAGNOSTIC_LOGGED = False

    def test_judge_uses_the_same_groq_call_path(self):
        _Handler.responses = [self._success({"duplicates": []})]
        with patch.object(groq, "GROQ_URL", self.url), patch.dict(
            "os.environ", {"GROQ_API_KEY": "gsk_dummy_key"}, clear=False
        ):
            result = dedupe._default_judge(
                [{"fact_text": "stored fact"}],
                [{"fact": "new fact", "fact_key": "new fact"}],
            )
        self.assertEqual(result, {"duplicates": []})
        self.assertEqual(_Handler.requests[0]["Authorization"], "Bearer gsk_dummy_key")

    def test_candidate_generation_uses_the_same_groq_call_path(self):
        _Handler.responses = [self._success({"candidates": []})]
        with patch.object(groq, "GROQ_URL", self.url), patch.dict(
            "os.environ", {"GROQ_API_KEY": "gsk_dummy_key"}, clear=False
        ):
            self.assertEqual(pipeline.call("candidate prompt"), {"candidates": []})
        self.assertEqual(_Handler.requests[0]["Authorization"], "Bearer gsk_dummy_key")

    def test_enrichment_stops_on_auth_failure_without_incrementing_attempts(self):
        _Handler.statuses = [401]
        _Handler.responses = [{"error": "invalid key"}]
        conn = connect(":memory:")
        init_schema(conn)
        for name in ("Otter", "Badger"):
            conn.execute(
                """INSERT INTO species
                (wikidata_qid, common_name, wikipedia_title, discovered_at, source)
                VALUES (?, ?, ?, '2020', 'wishlist')""",
                (name, name, name),
            )
        conn.commit()
        wiki = {"query": {"pages": {"1": {"extract": "x" * 500}}}}
        errors = io.StringIO()
        with patch.object(groq, "GROQ_URL", self.url), patch.dict(
            "os.environ",
            {"GROQ_API_KEY": "gsk_dummy_key", "PEXELS_API_KEY": "pexels"},
            clear=False,
        ), patch.object(enrich, "get_json", return_value=wiki), redirect_stderr(errors):
            enrich.enrich(conn, 2, clock=lambda: 0, sleeper=lambda _: None)
        rows = conn.execute(
            "SELECT enrichment_attempts, enrichment_status FROM species ORDER BY id"
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(0, "pending")] * 2)
        self.assertIn("::error::GROQ_API_KEY was rejected by Groq (HTTP 401).", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

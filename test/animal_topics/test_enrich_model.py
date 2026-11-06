import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from animal_topics import enrich, groq
from animal_topics.db import connect, init_schema


class _ModelHandler(BaseHTTPRequestHandler):
    models = []
    fallback_model = "llama-3.3-70b-versatile"
    reject_fallback = False

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        model = payload["model"]
        self.__class__.models.append(model)
        if model != self.fallback_model or self.__class__.reject_fallback:
            body = {"error": {"code": "model_not_found"}}
            status = 404
        else:
            body = {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "classes": ["Mammal"],
                            "habitats": ["Terrestrial"],
                            "visual_keywords": ["otter"],
                        })
                    },
                    "finish_reason": "stop",
                }]
            }
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_):
        pass


class EnrichModelTests(unittest.TestCase):
    def setUp(self):
        _ModelHandler.models = []
        _ModelHandler.reject_fallback = False
        groq._ENRICH_MODEL_FALLBACK = False
        groq._ENRICH_MODEL_UNAVAILABLE = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()

    def _connection(self):
        conn = connect(":memory:")
        init_schema(conn)
        conn.execute(
            """INSERT INTO species
            (wikidata_qid, common_name, wikipedia_title, discovered_at, source)
            VALUES ('Q1', 'Otter', 'Otter', '2020', 'discovery')"""
        )
        conn.commit()
        return conn

    def _run(self, conn, model=None):
        env = {
            "GROQ_API_KEY": "test-key",
            "PEXELS_API_KEY": "",
        }
        if model:
            env["GROQ_ENRICH_MODEL"] = model
        wiki = {"query": {"pages": {"1": {"extract": "Otter " + "text " * 120}}}}
        with patch.object(groq, "GROQ_URL", self.url), patch.dict(
            os.environ, env, clear=False
        ), patch.object(enrich, "get_json", return_value=wiki):
            enrich.enrich(conn, 1, sleeper=lambda _: None)

    def test_primary_404_retries_fallback_and_enriches(self):
        conn = self._connection()
        self._run(conn)
        row = conn.execute(
            "SELECT enrichment_status, enrichment_attempts FROM species"
        ).fetchone()
        self.assertEqual(tuple(row), ("done", 1))
        self.assertEqual(
            _ModelHandler.models,
            ["openai/gpt-oss-20b", "llama-3.3-70b-versatile"],
        )

    def test_both_models_404_stop_without_marking_failed(self):
        _ModelHandler.reject_fallback = True
        conn = self._connection()
        self._run(conn)
        row = conn.execute(
            "SELECT enrichment_status, enrichment_attempts FROM species"
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", 0))
        self.assertEqual(len(_ModelHandler.models), 2)

    def test_enrichment_model_override_is_used(self):
        conn = self._connection()
        self._run(conn, model="custom-enrichment-model")
        self.assertEqual(_ModelHandler.models[0], "custom-enrichment-model")


if __name__ == "__main__":
    unittest.main()

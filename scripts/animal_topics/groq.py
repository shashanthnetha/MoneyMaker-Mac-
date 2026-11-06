import json
import os
import sys

from .http import post_json

GROQ_URL = os.environ.get(
    "GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions"
)
_AUTH_DIAGNOSTIC_LOGGED = False
_QUOTA_EXHAUSTED = False
_MODEL_FALLBACK_LOGGED = False
_ENRICH_MODEL_FALLBACK = False
DEFAULT_ENRICH_MODEL = "openai/gpt-oss-20b"
ENRICH_MODEL_FALLBACK = "llama-3.3-70b-versatile"
_ENRICH_MODEL_UNAVAILABLE = False


def call(
    prompt,
    system="You are a YouTube Shorts strategist. Output only valid JSON.",
    return_metadata=False,
    model=None,
):
    global _AUTH_DIAGNOSTIC_LOGGED, _QUOTA_EXHAUSTED
    global _ENRICH_MODEL_FALLBACK, _MODEL_FALLBACK_LOGGED, _ENRICH_MODEL_UNAVAILABLE
    if _QUOTA_EXHAUSTED:
        result = {"result": None, "raw_content": "", "finish_reason": None,
                  "error": "quota_exhausted", "status": 429}
        return result if return_metadata else None
    if _ENRICH_MODEL_UNAVAILABLE and model == os.environ.get(
        "GROQ_ENRICH_MODEL", DEFAULT_ENRICH_MODEL
    ):
        result = {
            "result": None,
            "raw_content": "",
            "finish_reason": None,
            "error": "quota_exhausted",
            "status": 404,
        }
        return result if return_metadata else None
    raw_key = os.environ.get("GROQ_API_KEY", "")
    key = raw_key.strip()
    if not key:
        result = {
            "result": None,
            "raw_content": "",
            "finish_reason": None,
            "error": "http_error",
            "status": None,
        }
        return result if return_metadata else None

    request_headers = {"Authorization": "Bearer " + key}
    enrich_model = os.environ.get("GROQ_ENRICH_MODEL", DEFAULT_ENRICH_MODEL)
    is_enrichment = model == enrich_model
    requested_model = model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    if is_enrichment and _ENRICH_MODEL_FALLBACK:
        requested_model = ENRICH_MODEL_FALLBACK
    body = {
        "model": requested_model,
        "temperature": 0.8,
        "max_tokens": 1500 if requested_model.startswith("openai/gpt-oss") else 600,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if requested_model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
    response = post_json(
        GROQ_URL,
        body,
        request_headers,
        60,
        (5, 15, 30, 45),
    )
    if isinstance(response, dict) and response.get("_quota_exhausted"):
        _QUOTA_EXHAUSTED = True
        result = {"result": None, "raw_content": response.get("_raw_body", ""),
                  "finish_reason": None, "error": "quota_exhausted", "status": 429}
        return result if return_metadata else None
    if (
        isinstance(response, dict)
        and response.get("_http_error") == 404
        and "model_not_found" in response.get("_raw_body", "").casefold()
        and is_enrichment
    ):
        if requested_model == ENRICH_MODEL_FALLBACK:
            _ENRICH_MODEL_UNAVAILABLE = True
            print(
                f"enrich model {requested_model} not found, no fallback available",
                file=sys.stderr,
            )
            result = {
                "result": None,
                "raw_content": response.get("_raw_body", ""),
                "finish_reason": None,
                "error": "quota_exhausted",
                "status": 404,
            }
            return result if return_metadata else None
        _ENRICH_MODEL_FALLBACK = True
        print(
            f"enrich model {requested_model} not found, falling back to {ENRICH_MODEL_FALLBACK}",
            file=sys.stderr,
        )
        return call(prompt, system, return_metadata, enrich_model)
    if isinstance(response, dict) and response.get("_http_error") in (401, 403):
        if not _AUTH_DIAGNOSTIC_LOGGED:
            print(
                f"Groq auth diagnostics: key_length={len(key)} "
                f"starts_with_gsk={key.startswith('gsk_')} "
                f"had_surrounding_whitespace={raw_key != key} "
                f"header_starts_with_bearer={request_headers['Authorization'].startswith('Bearer ')}",
                file=sys.stderr,
            )
            _AUTH_DIAGNOSTIC_LOGGED = True
        result = {
            "result": None,
            "raw_content": response.get("_raw_body", ""),
            "finish_reason": None,
            "error": "auth_error",
            "status": response["_http_error"],
        }
        return result if return_metadata else None
    if response is None:
        result = {
            "result": None,
            "raw_content": "",
            "finish_reason": None,
            "error": "http_error",
            "status": None,
        }
        return result if return_metadata else None

    try:
        choice = response["choices"][0]
        content = choice["message"].get("content") or ""
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        content = ""
        finish_reason = None
    if not content:
        result = {
            "result": None,
            "raw_content": "",
            "finish_reason": finish_reason,
            "error": "empty_content",
            "status": 200,
        }
        return result if return_metadata else None
    try:
        start = content.index("{")
        end = content.rindex("}") + 1
        parsed = json.loads(content[start:end])
    except (ValueError, json.JSONDecodeError):
        parsed = None
    if not isinstance(parsed, dict):
        result = {
            "result": None,
            "raw_content": content,
            "finish_reason": finish_reason,
            "error": "no_json",
            "status": 200,
        }
    else:
        result = {
            "result": parsed,
            "raw_content": content,
            "finish_reason": finish_reason,
            "error": None,
            "status": 200,
        }
    return result if return_metadata else result["result"]

import json
import os
import sys

from .http import post_json

GROQ_URL = os.environ.get(
    "GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions"
)
_AUTH_DIAGNOSTIC_LOGGED = False


def call(
    prompt,
    system="You are a YouTube Shorts strategist. Output only valid JSON.",
    return_metadata=False,
):
    global _AUTH_DIAGNOSTIC_LOGGED
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
    response = post_json(
        GROQ_URL,
        {
            "model": "openai/gpt-oss-120b",
            "temperature": 0.8,
            "max_tokens": 1500,
            "reasoning_effort": "low",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        },
        request_headers,
        60,
        (5, 15, 30, 45),
    )
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

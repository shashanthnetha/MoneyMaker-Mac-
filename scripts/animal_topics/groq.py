import json
import os

from .http import post_json


def call(
    prompt,
    system="You are a YouTube Shorts strategist. Output only valid JSON.",
    return_metadata=False,
):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        result = {
            "result": None,
            "raw_content": "",
            "finish_reason": None,
            "error": "http_error",
        }
        return result if return_metadata else None
    response = post_json(
        "https://api.groq.com/openai/v1/chat/completions",
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
        {"Authorization": "******"},
        60,
        (5, 15, 30, 45),
    )
    if response is None:
        result = {
            "result": None,
            "raw_content": "",
            "finish_reason": None,
            "error": "http_error",
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
        }
    else:
        result = {
            "result": parsed,
            "raw_content": content,
            "finish_reason": finish_reason,
            "error": None,
        }
    return result if return_metadata else result["result"]

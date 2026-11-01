"""Small stdlib-only HTTP helpers."""
import json, re, sys, time, urllib.parse, urllib.request, urllib.error
UA = "MoneyMakerAnimalBot/1.0 (https://github.com/Anudeep-Reddy07/MoneyMaker-Automation)"

def _request(url, method="GET", body=None, headers=None, timeout=30, backoff=(2,5,10), attempts=None):
    hdr = {"User-Agent": UA}; hdr.update(headers or {})
    if attempts is None:
        attempts = len(backoff) + 1
    attempts = max(attempts, 5)
    delays = (0,) + tuple(backoff[: max(0, attempts - 1)])
    host = urllib.parse.urlparse(url).netloc
    skip_next_delay = False
    for i, delay in enumerate(delays):
        if delay and not skip_next_delay:
            time.sleep(delay)
        skip_next_delay = False
        started = time.monotonic()
        raw_body = ""
        try:
            req = urllib.request.Request(url, data=body, headers=hdr, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8")
                if response.status >= 500:
                    raise urllib.error.HTTPError(url, response.status, "server", response.headers, None)
                payload = json.loads(raw_body)
                if isinstance(payload, dict):
                    payload["_headers"] = dict(response.headers.items())
                elapsed = time.monotonic() - started
                if host == "qlever.cs.uni-freiburg.de":
                    print(f"SPARQL request succeeded: {host} elapsed {elapsed:.2f}s", file=sys.stderr)
                return payload
        except Exception as exc:
            elapsed = time.monotonic() - started
            status = getattr(exc, "code", None)
            detail = f"HTTP {status}" if status is not None else f"{type(exc).__name__}: {exc}"
            if not raw_body and hasattr(exc, "read"):
                try:
                    raw_body = exc.read().decode("utf-8")
                except Exception:
                    raw_body = ""
            print(
                f"HTTP request failed: {host} {detail} elapsed {elapsed:.2f}s"
                + (f" body={raw_body[:300]}" if raw_body else ""),
                file=sys.stderr,
            )
            if status in (401, 403):
                return {
                    "_http_error": status,
                    "_raw_body": raw_body,
                }
            if status in (400, 404):
                return {
                    "_http_error": status,
                    "_raw_body": raw_body,
                }
            if status == 429:
                wait = None
                match = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|s)", raw_body, re.I)
                if match:
                    wait = float(match.group(1)) / 1000 if match.group(2).lower() == "ms" else float(match.group(1))
                if "tokens per day" in raw_body.casefold() or "(tpd)" in raw_body.casefold() or (wait is not None and wait > 90):
                    return {
                        "_http_error": 429,
                        "_raw_body": raw_body,
                        "_quota_exhausted": True,
                    }
                if i == len(delays) - 1:
                    return None
                if wait is None:
                    wait = (5, 15, 30, 45)[min(i, 3)]
                time.sleep(wait + 1)
                skip_next_delay = True
                continue
            if i == len(delays) - 1:
                return None
    return None

def get_json(url, headers=None, params=None, timeout=30, attempts=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return _request(url, headers=headers, timeout=timeout, attempts=attempts)

def post_json(url, body, headers=None, timeout=30, backoff=(2,5,10), attempts=None):
    return _request(
        url,
        "POST",
        json.dumps(body).encode(),
        {"Content-Type":"application/json", **(headers or {})},
        timeout,
        backoff,
        attempts,
    )

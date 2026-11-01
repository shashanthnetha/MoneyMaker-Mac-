"""Small stdlib-only HTTP helpers."""
import json, sys, time, urllib.parse, urllib.request, urllib.error
UA = "MoneyMakerAnimalBot/1.0 (https://github.com/Anudeep-Reddy07/MoneyMaker-Automation)"

def _request(url, method="GET", body=None, headers=None, timeout=30, backoff=(2,5,10), attempts=None):
    hdr = {"User-Agent": UA}; hdr.update(headers or {})
    if attempts is None:
        attempts = len(backoff) + 1
    delays = (0,) + tuple(backoff[: max(0, attempts - 1)])
    host = urllib.parse.urlparse(url).netloc
    for i, delay in enumerate(delays):
        if delay: time.sleep(delay)
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
            if i == len(delays) - 1:
                return None
    return None

def get_json(url, headers=None, params=None, timeout=30, attempts=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return _request(url, headers=headers, timeout=timeout, attempts=attempts)

def post_json(url, body, headers=None, timeout=30, backoff=(2,5,10)):
    return _request(url, "POST", json.dumps(body).encode(), {"Content-Type":"application/json", **(headers or {})}, timeout, backoff)

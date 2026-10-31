import datetime
import json
import math
import re
import sys
import time
import urllib.parse

from .db import now_utc
from .http import get_json
from .wishlist import apply

ENDPOINT = "https://qlever.cs.uni-freiburg.de/api/wikidata"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
PAGE_SIZE = 200
MAX_PAGES = 6
WALL_CLOCK_SECONDS = 6 * 60
LIVING_QIDS = ("Q7432", "Q34740", "Q729", "Q237350", "Q23038290", "Q15978631")
EXPECTED_LABELS = {
    "Q7432": "species",
    "Q34740": "genus",
    "Q729": "Animalia",
    "Q237350": "critically endangered species",
    "Q23038290": "fossil taxon",
    "Q15978631": "Homo sapiens",
}
PREFIX = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
"""


def _request(fetch, url, params, timeout, attempts, headers=None):
    try:
        return fetch(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            attempts=attempts,
        )
    except TypeError:
        return fetch(url, params=params)


def _bindings(data):
    return (data or {}).get("results", {}).get("bindings", [])


def _value(binding, name):
    return binding.get(name, {}).get("value")


def ranked_query(kind, limit=PAGE_SIZE, offset=0):
    if kind == "living":
        body = f"""SELECT ?taxon ?name ?sci ?links WHERE {{
  ?taxon wdt:P105 wd:Q7432 ; wikibase:sitelinks ?links .
  FILTER(?links >= 3)
  OPTIONAL {{ ?taxon wdt:P1843 ?name . FILTER(LANG(?name) = "en") }}
  OPTIONAL {{ ?taxon wdt:P225 ?sci }}
}} ORDER BY DESC(?links) ?taxon ?name LIMIT {limit} OFFSET {offset}"""
    else:
        body = f"""SELECT ?taxon ?name ?sci ?links WHERE {{
  {{ ?taxon wdt:P141 wd:Q237350 }} UNION {{ ?taxon wdt:P31 wd:Q23038290 }}
  ?taxon wdt:P105 ?rank . FILTER(?rank IN (wd:Q7432, wd:Q34740))
  ?taxon wikibase:sitelinks ?links . FILTER(?links >= 3)
  OPTIONAL {{ ?taxon wdt:P1843 ?name . FILTER(LANG(?name) = "en") }}
  OPTIONAL {{ ?taxon wdt:P225 ?sci }}
}} ORDER BY DESC(?links) ?taxon ?name LIMIT {limit} OFFSET {offset}"""
    return PREFIX + body


def animal_filter_query(qids):
    values = " ".join(f"wd:{qid}" for qid in qids)
    return PREFIX + f"""SELECT ?taxon ?ext WHERE {{
  VALUES ?taxon {{ {values} }}
  ?taxon wdt:P171* wd:Q729 .
  BIND(EXISTS {{ ?taxon wdt:P141 wd:Q237350 }} ||
       EXISTS {{ ?taxon wdt:P31 wd:Q23038290 }} AS ?ext)
}}"""


def fetch_pageviews(title, fetch=get_json):
    end = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=89)
    encoded = urllib.parse.quote(title.replace(" ", "_"), safe="")
    data = _request(
        fetch,
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{encoded}/daily/{start:%Y%m%d}/{end:%Y%m%d}",
        {},
        30,
        2,
    ) or {}
    if "items" not in data:
        return None, 0.0
    total = sum(int(item.get("views", 0)) for item in data["items"])
    return total, math.log10(total + 1)


def fetch_titles(qids, fetch=get_json):
    titles = {}
    for start in range(0, len(qids), 50):
        batch = qids[start : start + 50]
        data = _request(
            fetch,
            WIKIDATA_API,
            {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "sitelinks",
                "sitefilter": "enwiki",
                "format": "json",
            },
            30,
            2,
        )
        if data is None:
            raise RuntimeError("Wikidata title lookup failed")
        for qid in batch:
            title = data.get("entities", {}).get(qid, {}).get("sitelinks", {}).get(
                "enwiki", {}
            ).get("title")
            if title:
                titles[qid] = title
    return titles


def validate_labels(fetch=get_json):
    data = _request(
        fetch,
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": "|".join(EXPECTED_LABELS),
            "props": "labels",
            "languages": "en",
            "format": "json",
        },
        30,
        2,
    )
    if data is None:
        print("WARNING: Wikidata label validation failed; continuing", file=sys.stderr)
        return
    for qid, expected in EXPECTED_LABELS.items():
        actual = (
            data.get("entities", {})
            .get(qid, {})
            .get("labels", {})
            .get("en", {})
            .get("value")
        )
        if actual and actual.casefold() != expected.casefold():
            raise RuntimeError(
                f"Wikidata label mismatch for {qid}: expected {expected!r}, got {actual!r}"
            )


def _insert(conn, row, title, extinct, source, pageview_fetch=get_json, common_name=None):
    qid = row["qid"]
    if conn.execute("SELECT 1 FROM species WHERE wikidata_qid=?", (qid,)).fetchone():
        return None
    common = common_name or row.get("name") or re.sub(r"\s+\([^)]*\)$", "", title)
    sci = row.get("sci")
    if sci and common.casefold() == sci.casefold():
        return None
    conn.execute(
        """INSERT INTO species
        (wikidata_qid, scientific_name, common_name, wikipedia_title, is_extinct,
         sitelinks, discovered_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            qid,
            sci,
            common,
            title,
            int(extinct),
            int(row.get("links") or 0),
            now_utc(),
            source,
        ),
    )
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    views, popularity = fetch_pageviews(title, pageview_fetch)
    conn.execute(
        "UPDATE species SET pageviews_90d=?, popularity=? WHERE id=?",
        (views, popularity, sid),
    )
    time.sleep(0.2)
    return sid


def _page_rows(kind, offset, fetch):
    page = _request(
        fetch,
        ENDPOINT,
        {"query": ranked_query(kind, PAGE_SIZE, offset), "format": "json"},
        60,
        1,
        {"Accept": "application/sparql-results+json"},
    )
    if page is None:
        raise RuntimeError("QLever request failed")
    rows = []
    seen = set()
    for index, binding in enumerate(_bindings(page)):
        qid = (_value(binding, "taxon") or "").rsplit("/", 1)[-1]
        if not qid or qid in seen:
            continue
        seen.add(qid)
        rows.append(
            {
                "qid": qid,
                "name": _value(binding, "name"),
                "sci": _value(binding, "sci"),
                "links": _value(binding, "links"),
                "_index": index,
            }
        )
    return rows


def _animal_rows(rows, kind, fetch):
    if not rows:
        return []
    data = _request(
        fetch,
        ENDPOINT,
        {"query": animal_filter_query([row["qid"] for row in rows]), "format": "json"},
        60,
        1,
        {"Accept": "application/sparql-results+json"},
    )
    if data is None:
        raise RuntimeError("QLever animal filter failed")
    ext = {_value(row, "taxon").rsplit("/", 1)[-1]: _value(row, "ext") == "true" for row in _bindings(data)}
    result = []
    for row in rows:
        if row["qid"] == "Q15978631" or row["qid"] not in ext:
            continue
        row = dict(row)
        row["extinct"] = ext[row["qid"]]
        if kind == "living" and row["extinct"]:
            continue
        result.append(row)
    return result


def discover(conn, max_new, wishlist_path=None, fetch=get_json, clock=time.monotonic):
    started = clock()
    validate_labels(fetch)
    if wishlist_path:
        apply(conn, wishlist_path)
    quotas = {"living": math.ceil(max_new * 0.8), "extinct": max_new - math.ceil(max_new * 0.8)}
    added = {"living": 0, "extinct": 0}
    offsets = {}
    for kind in ("living", "extinct"):
        if quotas[kind] <= 0:
            continue
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"{kind}_offset",)).fetchone()
        offset = int(row["value"]) if row else 0
        offsets[kind] = offset
        for _ in range(MAX_PAGES):
            if clock() - started >= WALL_CLOCK_SECONDS:
                break
            page_start = offset
            try:
                rows = _page_rows(kind, page_start, fetch)
                animals = _animal_rows(rows, kind, fetch)
                titles = fetch_titles([row["qid"] for row in animals], fetch)
                processed = 0
                stopped = False
                with conn:
                    for row_data in animals:
                        processed = row_data["_index"] + 1
                        title = titles.get(row_data["qid"])
                        if not title:
                            continue
                        sid = _insert(
                            conn,
                            row_data,
                            title,
                            row_data["extinct"],
                            f"wikidata_{kind}",
                            fetch,
                        )
                        if sid:
                            added[kind] += 1
                        if added[kind] >= quotas[kind]:
                            stopped = True
                            break
                    if stopped:
                        offset += processed
                    elif len(rows) < PAGE_SIZE:
                        offset += len(rows)
                    else:
                        offset += PAGE_SIZE
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        (f"{kind}_offset", str(offset)),
                    )
                offsets[kind] = offset
            except Exception as exc:
                print(f"{kind} discovery failed: {exc}", file=sys.stderr)
                break
            if added[kind] >= quotas[kind] or len(rows) < PAGE_SIZE:
                break
        if clock() - started >= WALL_CLOCK_SECONDS:
            break
    elapsed = clock() - started
    print(
        f"discovery summary: living added {added['living']}, extinct added {added['extinct']}, "
        f"offsets {offsets}, elapsed {elapsed:.1f}s",
        file=sys.stderr,
    )
    return added

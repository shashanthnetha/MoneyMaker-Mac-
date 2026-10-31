import json
import math
import os
import sys
import time
import urllib.parse

from .db import now_utc
from .http import get_json

ENDPOINT = "https://qlever.cs.uni-freiburg.de/api/wikidata"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
PREFIX = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
"""


def parse(path):
    result = []
    try:
        lines = open(path, encoding="utf-8")
    except OSError:
        return result
    with lines:
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 1)
            name = parts[0].strip()
            try:
                boost = float(parts[1].strip()) if len(parts) == 2 else 3.0
            except ValueError:
                boost = 3.0
            result.append((name, boost if boost > 0 else 3.0))
    return result


def _request(fetch, url, params, timeout=30, attempts=2, headers=None):
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


def animal_query(qid):
    return PREFIX + f"""SELECT ?taxon ?ext WHERE {{
  VALUES ?taxon {{ wd:{qid} }}
  ?taxon wdt:P171* wd:Q729 .
  BIND(EXISTS {{ ?taxon wdt:P141 wd:Q237350 }} ||
       EXISTS {{ ?taxon wdt:P31 wd:Q23038290 }} AS ?ext)
}}"""


def _pageview(title, fetch):
    end = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date() - __import__("datetime").timedelta(days=1)
    start = end - __import__("datetime").timedelta(days=89)
    encoded = urllib.parse.quote(title.replace(" ", "_"), safe="")
    data = _request(
        fetch,
        f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{encoded}/daily/{start:%Y%m%d}/{end:%Y%m%d}",
        {},
    ) or {}
    if "items" not in data:
        return None, 0.0
    total = sum(int(item.get("views", 0)) for item in data["items"])
    return total, math.log10(total + 1)


def _title(fetch, qid):
    data = _request(
        fetch,
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "sitelinks",
            "sitefilter": "enwiki",
            "format": "json",
        },
    )
    if data is None:
        raise RuntimeError("Wikidata title lookup failed")
    return (
        data.get("entities", {})
        .get(qid, {})
        .get("sitelinks", {})
        .get("enwiki", {})
        .get("title")
    )


def resolve_entry(conn, name, search=None, sparql=None, pageviews=None):
    search = search or get_json
    sparql = sparql or get_json
    data = _request(
        search,
        WIKIDATA_API,
        {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "type": "item",
            "limit": 5,
            "format": "json",
        },
    ) or {}
    for item in data.get("search", []):
        qid = item.get("id")
        if not qid:
            continue
        result = _request(
            sparql,
            ENDPOINT,
            {"query": animal_query(qid), "format": "json"},
            60,
            1,
            {"Accept": "application/sparql-results+json"},
        )
        if not result or not result.get("results", {}).get("bindings"):
            continue
        title = _title(search, qid)
        if not title:
            continue
        binding = result["results"]["bindings"][0]
        extinct = binding.get("ext", {}).get("value") == "true"
        conn.execute(
            """INSERT OR IGNORE INTO species
            (wikidata_qid, scientific_name, common_name, wikipedia_title, is_extinct,
             sitelinks, discovered_at, source)
            VALUES (?, NULL, ?, ?, ?, 0, ?, 'wishlist')""",
            (qid, name, title, int(extinct), now_utc()),
        )
        row = conn.execute("SELECT * FROM species WHERE wikidata_qid=?", (qid,)).fetchone()
        if pageviews:
            views, popularity = pageviews(title)
        else:
            views, popularity = _pageview(title, search)
        conn.execute(
            "UPDATE species SET pageviews_90d=?, popularity=? WHERE id=?",
            (views, popularity, row["id"]),
        )
        time.sleep(0.2)
        return row["id"]
    return None


def apply(conn, path, resolve=None):
    entries = parse(path)
    unresolved = []
    attempts = 0
    for name, boost in entries:
        row = conn.execute(
            "SELECT id FROM species WHERE lower(common_name)=lower(?)", (name,)
        ).fetchone()
        if row:
            row_id = row["id"]
        elif attempts < 25:
            attempts += 1
            row_id = resolve(conn, name) if resolve else resolve_entry(conn, name)
            if hasattr(row_id, "keys"):
                row_id = row_id["id"]
        else:
            row_id = None
        if row_id is None:
            unresolved.append(name)
        else:
            conn.execute("UPDATE species SET boost=? WHERE id=?", (boost, row_id))
    values = {name.casefold(): boost for name, boost in entries}
    for row in conn.execute("SELECT id,common_name FROM species"):
        conn.execute(
            "UPDATE species SET boost=? WHERE id=?",
            (values.get(row["common_name"].casefold(), 1.0), row["id"]),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('wishlist_unresolved',?)",
        (json.dumps(unresolved),),
    )
    conn.commit()
    for name in unresolved:
        print(f"wishlist unresolved: {name}", file=sys.stderr)
    return unresolved

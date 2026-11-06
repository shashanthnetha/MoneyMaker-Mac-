import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import re

from .db import CLASSES, HABITATS
from .discovery import choose_common_name, is_human_lineage, repair_common_names
from .http import get_json
from .groq import call

CLASS_MAP = {
    alias: value
    for value, aliases in {
        "Mammal": ("mammal", "mammals", "mammalia"),
        "Bird": ("bird", "birds", "aves"),
        "Reptile": ("reptile", "reptiles", "reptilia"),
        "Amphibian": ("amphibian", "amphibians", "amphibia"),
        "Fish": ("fish", "fishes", "actinopterygii", "chondrichthyes"),
        "Insect": ("insect", "insects", "insecta"),
        "Arachnid": ("arachnid", "arachnids", "arachnida"),
        "Crustacean": ("crustacean", "crustaceans", "malacostraca"),
        "Mollusc": (
            "mollusc",
            "mollusk",
            "molluscs",
            "mollusks",
            "gastropod",
            "bivalve",
        ),
        "Cephalopod": ("cephalopod", "cephalopods"),
        "Cnidarian": ("cnidarian", "cnidarians", "jellyfish"),
        "Dinosaur": ("dinosaur", "dinosaurs"),
        "Other Invertebrate": ("invertebrate", "other invertebrate"),
    }.items()
    for alias in aliases
}
HABITAT_MAP = {
    alias: value
    for value, aliases in {
        "Aquatic": ("aquatic", "marine", "freshwater", "water"),
        "Terrestrial": ("terrestrial", "land", "arboreal", "ground"),
        "Aerial": ("aerial", "flying", "air"),
    }.items()
    for alias in aliases
}


def normalize_values(values, mapping):
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, str):
            mapped = mapping.get(value.strip().casefold())
            if mapped and mapped not in result:
                result.append(mapped)
    return result


def normalize_keywords(values):
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, str):
            value = value.strip()
            if value and value not in result:
                result.append(value[:60])
        if len(result) == 3:
            break
    return result


def _failure(conn, row, reason, metadata):
    print(f"enrichment failed {row['common_name']}: {reason}", file=sys.stderr)
    if reason != "http_error":
        print(
            f"raw={metadata.get('raw_content', '')[:300]!r} "
            f"finish_reason={metadata.get('finish_reason')!r}",
            file=sys.stderr,
        )
        conn.execute(
            "UPDATE species SET enrichment_status='failed', enrichment_attempts=enrichment_attempts+1 WHERE id=?",
            (row["id"],),
        )
    else:
        conn.execute(
            "UPDATE species SET enrichment_status='failed' WHERE id=?",
            (row["id"],),
        )
    conn.commit()


def _prompt(row, text):
    return f"""Animal: {row["common_name"]}
Extinct: {bool(row["is_extinct"])}
SOURCE TEXT:
{text[:900]}

Allowed classes: Mammal, Bird, Reptile, Amphibian, Fish, Insect, Arachnid, Crustacean, Mollusc, Cephalopod, Cnidarian, Dinosaur, Other Invertebrate.
Allowed habitats: Aquatic, Terrestrial, Aerial.
Example: {{"classes":["Mammal"],"habitats":["Terrestrial"],"visual_keywords":["african lion","lion pride savanna"]}}
Living species visual_keywords must include the common name plus up to two closely related real search terms. Extinct species must never use the species name; use a closest living relative, museum skeleton, fossil, or similar real stock-footage subjects.
Return ONLY the JSON object"""


INTERESTING_HEADINGS = (
    "behaviour", "behavior", "ecology", "diet", "feeding", "hunting",
    "predation", "reproduction", "breeding", "life cycle", "description",
    "anatomy", "physiology", "senses", "communication", "intelligence",
    "adaptation", "defence", "defense", "habitat", "lifespan", "social",
)


def build_wiki_text(full_text):
    """Keep the lead and behavior-rich sections within the prompt budget."""
    lines = full_text.splitlines()
    first_heading = next(
        (index for index, line in enumerate(lines) if re.match(r"^==+\s*(.+?)\s*==+$", line)),
        len(lines),
    )
    lead = "\n".join(lines[:first_heading]).strip()[:900]
    sections = []
    index = first_heading
    while index < len(lines):
        match = re.match(r"^==+\s*(.+?)\s*==+$", lines[index])
        if not match:
            index += 1
            continue
        heading = match.group(1).strip()
        end = index + 1
        while end < len(lines) and not re.match(r"^==+\s*(.+?)\s*==+$", lines[end]):
            end += 1
        if any(term in heading.casefold() for term in INTERESTING_HEADINGS):
            sections.append(f"{lines[index]}\n" + "\n".join(lines[index + 1:end]).strip()[:900])
        index = end
    selected = lead
    for section in sections:
        candidate = f"{selected}\n\n{section}".strip()
        if len(candidate) > 5500:
            break
        selected = candidate
    return selected if len(selected) >= 1500 else full_text[:5500]


def _fetch_article(title):
    data = get_json(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "redirects": "1",
            "format": "json",
            "titles": title,
        },
    )
    if data is None:
        return None, None
    pages = data.get("query", {}).get("pages", {})
    full_text = next(iter(pages.values()), {}).get("extract", "")
    return full_text, build_wiki_text(full_text)


def _pexels_search(api_key, query, max_attempts=4):
    """Run a Pexels search, retrying non-throttle failures."""
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": 1}
    )
    delays = (0, 2, 5, 10)[:max_attempts]
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(
            url,
            headers={"Authorization": api_key},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
                remaining = response.headers.get("X-Ratelimit-Remaining")
                return {
                    "ok": True,
                    "throttled": False,
                    "payload": payload,
                    "remaining": int(remaining) if remaining is not None else None,
                    "attempts": attempt + 1,
                }
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8")
            except Exception:
                pass
            if exc.code == 429:
                return {
                    "ok": False,
                    "throttled": True,
                    "payload": None,
                    "remaining": None,
                    "attempts": attempt + 1,
                }
            if exc.code < 500 or attempt == len(delays) - 1:
                return {
                    "ok": False,
                    "throttled": False,
                    "payload": None,
                    "remaining": None,
                    "attempts": attempt + 1,
                }
        except (OSError, ValueError):
            if attempt == len(delays) - 1:
                return {
                    "ok": False,
                    "throttled": False,
                    "payload": None,
                    "remaining": None,
                    "attempts": attempt + 1,
                }
    return {
        "ok": False,
        "throttled": False,
        "payload": None,
        "remaining": None,
        "attempts": len(delays),
    }


def _stop_pexels(checked, pending):
    print(
        f"pexels throttled: stopping footage checks ({checked} checked, {pending} pending)",
        file=sys.stderr,
    )
    print(
        "::warning::Pexels rate limit reached; remaining species will be checked in the next run.",
        file=sys.stderr,
    )


def enrich(conn, batch, clock=time.monotonic, sleeper=time.sleep):
    repair_common_names(conn)
    conn.execute(
        """UPDATE species SET status='disabled', disabled_reason='human_lineage'
        WHERE status='active' AND (
          lower(COALESCE(scientific_name, '')) LIKE 'homo %' OR
          lower(common_name) LIKE '%neanderthal%' OR lower(wikipedia_title) LIKE '%neanderthal%' OR
          lower(common_name) LIKE '%denisovan%' OR lower(wikipedia_title) LIKE '%denisovan%' OR
          lower(common_name) LIKE '%heidelberg man%' OR lower(wikipedia_title) LIKE '%heidelberg man%' OR
          lower(common_name) LIKE '%caveman%' OR lower(wikipedia_title) LIKE '%caveman%' OR
          lower(common_name) LIKE '%cave man%' OR lower(wikipedia_title) LIKE '%cave man%' OR
          lower(common_name) LIKE '%hominin%' OR lower(wikipedia_title) LIKE '%hominin%' OR
          lower(common_name) LIKE '%hominid%' OR lower(wikipedia_title) LIKE '%hominid%' OR
          lower(common_name) LIKE '%homo %' OR lower(wikipedia_title) LIKE '%homo %' OR
          lower(common_name) LIKE '%human%' OR lower(wikipedia_title) LIKE '%human%'
        )"""
    )
    if not conn.execute("SELECT 1 FROM meta WHERE key='enrich_reset_v2'").fetchone():
        conn.execute(
            """UPDATE species SET enrichment_attempts=0, enrichment_status='pending'
            WHERE enrichment_status='failed' AND status='active'"""
        )
        conn.execute("INSERT INTO meta(key,value) VALUES('enrich_reset_v2','1')")
    if not conn.execute("SELECT 1 FROM meta WHERE key='enrich_reset_v3'").fetchone():
        conn.execute(
            """UPDATE species SET status='active', disabled_reason=NULL, footage_score=NULL
            WHERE disabled_reason='low_footage'
              AND (footage_score IS NULL OR footage_score=0)"""
        )
        conn.execute("INSERT INTO meta(key,value) VALUES('enrich_reset_v3','1')")
    conn.commit()

    if not conn.execute("SELECT 1 FROM meta WHERE key='text_refresh_v1'").fetchone():
        refresh_rows = conn.execute(
            "SELECT id, wikipedia_title, wiki_text FROM species WHERE enrichment_status='done'"
        ).fetchall()
        for index, row in enumerate(refresh_rows):
            if index:
                time.sleep(0.3)
            try:
                _, refreshed = _fetch_article(row["wikipedia_title"])
                if refreshed is not None:
                    conn.execute(
                        "UPDATE species SET wiki_text=? WHERE id=?",
                        (refreshed, row["id"]),
                    )
            except Exception:
                continue
        conn.execute("INSERT INTO meta(key,value) VALUES('text_refresh_v1','1')")
        conn.commit()

    if os.getenv("GROQ_API_KEY"):
        rows = conn.execute(
            """SELECT * FROM species
            WHERE enrichment_status IN ('pending','failed') AND enrichment_attempts<3
            ORDER BY CASE WHEN boost>1 THEN 0 ELSE 1 END, discovered_at LIMIT ?""",
            (batch,),
        ).fetchall()
        last_groq_start = None
        last_groq_status = None
        for row in rows:
            try:
                full_text, text = _fetch_article(row["wikipedia_title"])
                if full_text is None:
                    _failure(conn, row, "http_error", {})
                    continue
                if len(full_text) < 400:
                    _failure(
                        conn,
                        row,
                        "empty_content",
                        {"raw_content": full_text, "finish_reason": None},
                    )
                    continue
                if last_groq_start is not None and last_groq_status in (200, 429):
                    remaining = 8 - (clock() - last_groq_start)
                    if remaining > 0:
                        sleeper(remaining)
                last_groq_start = clock()
                metadata = call(
                    _prompt(row, text),
                    return_metadata=True,
                    model=os.environ.get("GROQ_ENRICH_MODEL", "openai/gpt-oss-20b"),
                )
                last_groq_status = metadata.get("status")
                if metadata.get("error") == "auth_error":
                    print(
                        f"::error::GROQ_API_KEY was rejected by Groq (HTTP {metadata['status']}). "
                        "Update the GitHub secret.",
                        file=sys.stderr,
                    )
                    return
                if metadata.get("error") == "quota_exhausted":
                    print(
                        "::warning::Groq daily token quota exhausted; remaining species will be enriched in a later run.",
                        file=sys.stderr,
                    )
                    break
                reason = metadata.get("error")
                if reason:
                    _failure(conn, row, reason, metadata)
                    continue
                result = metadata.get("result") or {}
                if not all(key in result for key in ("classes", "habitats", "visual_keywords")):
                    _failure(conn, row, "missing_keys", metadata)
                    continue
                classes = normalize_values(result["classes"], CLASS_MAP)
                habitats = normalize_values(result["habitats"], HABITAT_MAP)
                keywords = normalize_keywords(result["visual_keywords"])
                if not classes:
                    _failure(conn, row, "no_valid_class", metadata)
                    continue
                if not habitats:
                    _failure(conn, row, "no_valid_habitat", metadata)
                    continue
                if not keywords:
                    _failure(conn, row, "no_keywords", metadata)
                    continue
                with conn:
                    common_name = row["common_name"]
                    if row["source"] != "wishlist":
                        common_name = choose_common_name(
                            row["common_name"],
                            row["wikipedia_title"],
                            row["scientific_name"],
                            text[:900],
                        )
                    conn.execute(
                        """UPDATE species SET wiki_text=?, common_name=?, visual_keywords=?,
                        enrichment_status='done',
                        enrichment_attempts=enrichment_attempts+1
                        WHERE id=?""",
                        (text, common_name, json.dumps(keywords), row["id"]),
                    )
                    for name in classes + habitats + (
                        ["Extinct"] if row["is_extinct"] else ["Living"]
                    ):
                        gid = conn.execute(
                            "SELECT id FROM groups WHERE name=?", (name,)
                        ).fetchone()["id"]
                        conn.execute(
                            "INSERT OR IGNORE INTO species_groups(species_id,group_id) VALUES(?,?)",
                            (row["id"], gid),
                        )
            except Exception:
                _failure(conn, row, "http_error", {})
    else:
        print("enrichment skipped: missing GROQ_API_KEY", file=sys.stderr)

    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        print("pexels skipped: missing PEXELS_API_KEY", file=sys.stderr)
        return

    candidates = conn.execute(
        """SELECT * FROM species
        WHERE enrichment_status='done' AND status='active'
          AND footage_score IS NULL AND visual_keywords IS NOT NULL
        ORDER BY CASE WHEN source='wishlist' THEN 0 ELSE 1 END,
                 popularity IS NULL, popularity DESC, id"""
    ).fetchall()
    checked = 0
    requests = 0
    for row in candidates:
        if requests >= 100:
            _stop_pexels(checked, len(candidates) - checked)
            return
        try:
            keywords = json.loads(row["visual_keywords"])
        except (TypeError, json.JSONDecodeError):
            checked += 1
            continue
        if not isinstance(keywords, list):
            checked += 1
            continue
        queries = keywords if row["is_extinct"] else [row["common_name"]] + keywords
        queries = queries[:2]
        scores = []
        low_remaining = False
        failed = False
        for query in queries:
            if requests >= 100:
                _stop_pexels(checked, len(candidates) - checked)
                return
            if requests:
                sleeper(1)
            response = _pexels_search(
                api_key, query, max_attempts=max(1, 100 - requests)
            )
            requests += response["attempts"]
            if response["throttled"]:
                _stop_pexels(checked, len(candidates) - checked)
                return
            if not response["ok"]:
                failed = True
                break
            scores.append(int(response["payload"].get("total_results", 0)))
            if response["remaining"] is not None and response["remaining"] < 25:
                low_remaining = True
            if scores[-1] >= 15:
                break
        if failed or not scores:
            checked += 1
            continue
        score = max(scores)
        with conn:
            conn.execute(
                """UPDATE species SET footage_score=?,
                status=CASE WHEN ? < 15 THEN 'disabled' ELSE status END,
                disabled_reason=CASE WHEN ? < 15 THEN 'low_footage' ELSE disabled_reason END
                WHERE id=?""",
                (score, score, score, row["id"]),
            )
        checked += 1
        if low_remaining:
            _stop_pexels(checked, len(candidates) - checked)
            return

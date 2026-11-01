import json
import os
import sys
import time

from .db import CLASSES, HABITATS
from .discovery import is_human_lineage
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
{text}

Allowed classes: Mammal, Bird, Reptile, Amphibian, Fish, Insect, Arachnid, Crustacean, Mollusc, Cephalopod, Cnidarian, Dinosaur, Other Invertebrate.
Allowed habitats: Aquatic, Terrestrial, Aerial.
Example: {{"classes":["Mammal"],"habitats":["Terrestrial"],"visual_keywords":["african lion","lion pride savanna"]}}
Living species visual_keywords must include the common name plus up to two closely related real search terms. Extinct species must never use the species name; use a closest living relative, museum skeleton, fossil, or similar real stock-footage subjects.
Return ONLY the JSON object"""


def enrich(conn, batch, clock=time.monotonic, sleeper=time.sleep):
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
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('enrich_reset_v2','1')"
        )
    conn.commit()
    if not os.getenv("GROQ_API_KEY") or not os.getenv("PEXELS_API_KEY"):
        print("enrichment skipped: missing API key", file=sys.stderr)
        return
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
            data = get_json(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "extracts",
                    "explaintext": "1",
                    "exchars": "2500",
                    "redirects": "1",
                    "format": "json",
                    "titles": row["wikipedia_title"],
                },
            )
            if data is None:
                _failure(conn, row, "http_error", {})
                continue
            pages = data.get("query", {}).get("pages", {})
            text = next(iter(pages.values()), {}).get("extract", "")
            if len(text) < 400:
                _failure(conn, row, "empty_content", {"raw_content": text, "finish_reason": None})
                continue
            if last_groq_start is not None and last_groq_status in (200, 429):
                remaining = 8 - (clock() - last_groq_start)
                if remaining > 0:
                    sleeper(remaining)
            last_groq_start = clock()
            metadata = call(_prompt(row, text), return_metadata=True)
            last_groq_status = metadata.get("status")
            if metadata.get("error") == "auth_error":
                print(
                    f"::error::GROQ_API_KEY was rejected by Groq (HTTP {metadata['status']}). "
                    "Update the GitHub secret.",
                    file=sys.stderr,
                )
                return
            reason = metadata.get("error")
            if reason:
                _failure(conn, row, reason, metadata)
                continue
            result = metadata.get("result") or {}
            if "classes" not in result or "habitats" not in result or "visual_keywords" not in result:
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
            score = 0
            for keyword in keywords:
                response = get_json(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": os.environ["PEXELS_API_KEY"]},
                    params={"query": keyword, "per_page": 1},
                ) or {}
                remaining = response.get("_headers", {}).get(
                    "X-Ratelimit-Remaining", response.get("X-Ratelimit-Remaining")
                )
                if remaining is not None and int(remaining) < 25:
                    raise StopIteration("Pexels rate limit remaining below 25")
                score = max(score, int(response.get("total_results", 0)))
            if not row["is_extinct"]:
                response = get_json(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": os.environ["PEXELS_API_KEY"]},
                    params={"query": row["common_name"], "per_page": 1},
                ) or {}
                score = max(score, int(response.get("total_results", 0)))
            with conn:
                conn.execute(
                    """UPDATE species SET wiki_text=?, visual_keywords=?,
                    footage_score=?, enrichment_status='done',
                    enrichment_attempts=enrichment_attempts+1,
                    status=CASE WHEN ?<15 THEN 'disabled' ELSE status END,
                    disabled_reason=CASE WHEN ?<15 THEN 'low_footage' ELSE disabled_reason END
                    WHERE id=?""",
                    (text, json.dumps(keywords), score, score, score, row["id"]),
                )
                for name in classes + habitats + (["Extinct"] if row["is_extinct"] else ["Living"]):
                    gid = conn.execute(
                        "SELECT id FROM groups WHERE name=?", (name,)
                    ).fetchone()["id"]
                    conn.execute(
                        "INSERT OR IGNORE INTO species_groups(species_id,group_id) VALUES(?,?)",
                        (row["id"], gid),
                    )
        except StopIteration as exc:
            print(str(exc), file=sys.stderr)
            return
        except Exception:
            _failure(conn, row, "http_error", {})

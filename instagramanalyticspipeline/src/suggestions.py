"""
Optional, non-authoritative Suggested_Partnership heuristic.

Scans caption @mentions, #hashtags, and collaborator usernames against
brand_keywords.json. Never writes to Partnership -- only Suggested_Partnership,
which a human reviews and promotes manually via classify.py (or directly in
BigQuery) when correct.
"""
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"@([A-Za-z0-9_.]+)")
_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

_BRAND_KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "brand_keywords.json"


def load_brand_keywords(path: Path = _BRAND_KEYWORDS_PATH) -> dict:
    with open(path) as f:
        raw = json.load(f)
    return {k.lower(): v for k, v in raw.items() if not k.startswith("_")}


def _normalize(token: str) -> str:
    return token.lower().replace(".", "").replace("-", "")


def suggest_partnership(caption: str, collaborators: list, brand_map: dict) -> str | None:
    candidates = []
    candidates.extend(collaborators or [])
    if caption:
        candidates.extend(_MENTION_RE.findall(caption))
        candidates.extend(_HASHTAG_RE.findall(caption))

    for token in candidates:
        normalized = _normalize(token)
        if normalized in brand_map:
            return brand_map[normalized]
        # also try without trailing digits/underscores (e.g. "ferrero_it")
        stripped = normalized.split("_")[0]
        if stripped in brand_map:
            return brand_map[stripped]

    return None

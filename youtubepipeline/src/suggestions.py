"""
Optional, non-authoritative Suggested_Partnership heuristic. Same logic as
instagramanalyticspipeline/src/suggestions.py, reusing that pipeline's
brand_keywords.json as the single source of truth for brand mentions --
your partner list doesn't change per platform. Matched against the video
title (YouTube doesn't expose captions/descriptions with @mentions in the
same way Instagram/Facebook do, so titles are the best signal here).
"""
import json
import re
from pathlib import Path

_MENTION_RE = re.compile(r"@([A-Za-z0-9_.]+)")
_HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]+)")

_BRAND_KEYWORDS_PATH = (
    Path(__file__).resolve().parents[2] / "instagramanalyticspipeline" / "brand_keywords.json"
)


def load_brand_keywords(path: Path = _BRAND_KEYWORDS_PATH) -> dict:
    with open(path) as f:
        raw = json.load(f)
    return {k.lower(): v for k, v in raw.items() if not k.startswith("_")}


def _normalize(token: str) -> str:
    return token.lower().replace(".", "").replace("-", "")


def suggest_partnership(text: str, brand_map: dict) -> str | None:
    candidates = []
    if text:
        candidates.extend(_MENTION_RE.findall(text))
        candidates.extend(_HASHTAG_RE.findall(text))

    for token in candidates:
        normalized = _normalize(token)
        if normalized in brand_map:
            return brand_map[normalized]
        stripped = normalized.split("_")[0]
        if stripped in brand_map:
            return brand_map[stripped]

    return None

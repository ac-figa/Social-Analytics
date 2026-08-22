"""
Cross-platform content matching.

Suggests which content_items (across different platforms) are likely the
same underlying video, so they can be grouped into one Content_Group and
reported on together as a single piece of content.

Never fully authoritative: a pairwise match only clears AUTO_CONFIRM_SCORE
becomes a confirmed group membership automatically; anything between
MIN_SUGGEST_SCORE and AUTO_CONFIRM_SCORE is written as an *unconfirmed*
suggestion for a human to accept or reject. A human can always regroup,
split, or manually link items regardless of what this module decided --
see content_store.set_group_members / content_store.confirm_membership.
"""
import re
from datetime import datetime

# A pairwise score below this is not even suggested.
MIN_SUGGEST_SCORE = 0.55
# A pairwise score at or above this is auto-confirmed into a group without
# waiting for human review.
AUTO_CONFIRM_SCORE = 0.82

# Cross-posts of the same video across platforms are almost always
# published within a few days of each other. Beyond this, date proximity
# contributes nothing to the score.
MAX_DATE_DELTA_DAYS = 5

_MENTION_HASHTAG_RE = re.compile(r"[@#]\w+")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def normalize_caption(caption: str) -> str:
    """Strip @mentions/#hashtags (platform-specific noise) and punctuation,
    collapse whitespace, lowercase -- leaves just the words worth comparing."""
    if not caption:
        return ""
    text = _MENTION_HASHTAG_RE.sub("", caption)
    text = _PUNCTUATION_RE.sub("", text)
    return " ".join(text.lower().split())


def _caption_similarity(a: str, b: str) -> float:
    """Word-overlap similarity, as an *overlap coefficient*
    (intersection / smaller set's size) rather than Jaccard
    (intersection / union). Confirmed live against this project's real
    data (Aug 2026) that creators reword captions substantially per
    platform -- e.g. IG "There's two types of Italy trip" vs. TikTok
    "Which type of Italy trip do you prefer?" for the same video, posted
    8 minutes apart. Jaccard's union in the denominator meant a short,
    tightly-reworded caption got diluted by every word unique to the
    *other* platform's phrasing, even when every one of its own words
    matched -- scoring that real pair at 0.27. The overlap coefficient
    instead asks "how much of the smaller caption's content is present in
    the other," scoring the same pair at 0.50 and correctly clearing
    MIN_SUGGEST_SCORE once combined with the date signal.

    Character-level diffing (e.g. difflib) was tried first and rejected
    for a different reason -- it gives short, unrelated captions a
    deceptively high score just from shared spaces/common letters."""
    na, nb = normalize_caption(a), normalize_caption(b)
    words_a, words_b = set(na.split()), set(nb.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _date_proximity(a: datetime, b: datetime) -> float:
    """1.0 for identical timestamps, decaying linearly to 0.0 at
    MAX_DATE_DELTA_DAYS apart."""
    if a is None or b is None:
        return 0.0
    delta_days = abs((a - b).total_seconds()) / 86400
    if delta_days > MAX_DATE_DELTA_DAYS:
        return 0.0
    return 1.0 - (delta_days / MAX_DATE_DELTA_DAYS)


def pair_score(item_a: dict, item_b: dict) -> float:
    """item_*: {"Content_ID":..., "Platform":..., "Caption":...,
    "Publish_Date": datetime or None}.

    Same-platform pairs always score 0 -- matching links the same piece of
    content across *different* platforms, not near-duplicate posts on one
    platform.
    """
    if item_a.get("Platform") == item_b.get("Platform"):
        return 0.0

    cap_score = _caption_similarity(item_a.get("Caption"), item_b.get("Caption"))
    date_score = _date_proximity(item_a.get("Publish_Date"), item_b.get("Publish_Date"))

    # Caption match carries almost all the signal. Date proximity alone is
    # never enough on its own -- lots of unrelated posts land on the same
    # day -- so it only nudges an already-plausible caption match.
    return 0.75 * cap_score + 0.25 * date_score


def find_candidate_groups(items: list[dict]) -> list[dict]:
    """Greedy clustering of ungrouped content_items into candidate groups.

    Returns a list of:
      {"content_ids": [...], "confidence": float, "auto_confirm": bool}

    Each item lands in at most one candidate cluster (its best-scoring
    match chain), and a cluster never contains two items from the same
    platform -- "the same video on each platform" means one member per
    platform. Ambiguous items that don't clear MIN_SUGGEST_SCORE with
    anything are simply left out, for manual grouping in the dashboard.
    """
    n = len(items)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            score = pair_score(items[i], items[j])
            if score >= MIN_SUGGEST_SCORE:
                pairs.append((score, i, j))

    # Merge highest-confidence pairs first so a strong match wins over a
    # weaker one when an item is plausibly close to more than one other.
    pairs.sort(reverse=True)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def platforms_in(root: int) -> set:
        return {items[k]["Platform"] for k in range(n) if find(k) == root}

    cluster_min_score: dict[int, float] = {}
    for score, i, j in pairs:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        if platforms_in(ri) & platforms_in(rj):
            continue  # would put two items from the same platform together
        parent[ri] = rj
        merged_root = find(j)
        prior = min(cluster_min_score.get(ri, score), cluster_min_score.get(rj, score))
        cluster_min_score[merged_root] = min(prior, score)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    results = []
    for root, indices in clusters.items():
        if len(indices) < 2:
            continue
        confidence = cluster_min_score.get(root, MIN_SUGGEST_SCORE)
        results.append(
            {
                "content_ids": [items[k]["Content_ID"] for k in indices],
                "confidence": round(confidence, 3),
                "auto_confirm": confidence >= AUTO_CONFIRM_SCORE,
            }
        )
    return results

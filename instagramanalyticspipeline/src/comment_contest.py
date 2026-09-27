"""
Turns a "comment your favorite ___" Instagram post into a clean, ranked
vote count -- built for the tiramisu-spot giveaway post, but works for
any single-post comment contest.

Pulls every top-level comment on one post, then runs two separate Claude
passes rather than trying to do everything in one shot:

  1. Per-comment extraction: read each comment's raw text and pull out
     every distinct place name it actually names, exactly as the
     commenter spelled it. A comment can yield 0 places ("my nonna's
     house", pure emojis, a joke, tagging a friend with no place named)
     or more than one ("Spot A or Spot B!" -> two separate votes).
  2. Cross-comment normalization: the ~hundreds of distinct raw strings
     from step 1 (not the ~1000 raw comments) get grouped, so "Tiramisu
     Bar", "the tiramisu bar downtown", and "tiramisubar" can be
     recognized as the same place -- something no single isolated
     comment-by-comment pass could do, since it has no visibility into
     how other commenters spelled the same place. This itself is two
     steps: a deterministic mechanical pass first (_mechanical_key())
     collapses "@handle" vs. "plain name" duplicates for the exact same
     place -- a fact about the string, not a judgment call, and one a
     single Claude call over hundreds of entries missed several of when
     left to it -- and only the resulting (much smaller) deduplicated
     list goes to Claude for genuine typo/spelling-variant grouping.

The actual vote tally is plain Python counting over Claude's
categorical output (place name per comment) -- never derived arithmetic
asked of the model, same discipline as vision.py's demographics
percentages and hook_insights.py's style-pattern averages elsewhere in
this codebase.

Optionally also pulls comments from a cross-posted Facebook video of the
same content (--facebook-permalink / --facebook-video-id) and folds them
into the same extraction/grouping/tally pass, so a place mentioned on
one platform and spelled differently on the other still ends up counted
as one place. Facebook's own Page video permalink embeds its numeric
video ID directly in the URL (unlike Instagram's opaque shortcode), so
this needs no separate Facebook credentials or BigQuery lookup -- it
reuses the same META_ACCESS_TOKEN already loaded for Instagram, since
both pipelines share that same Meta App/System User token (see
facebookpipeline/src/config.py's docstring).

Requires ANTHROPIC_API_KEY in .env (already used by hook_analysis.py).

Run:
  python -m src.comment_contest --permalink https://www.instagram.com/p/SHORTCODE/
  python -m src.comment_contest --post-id 17851234567890123 --out-dir ~/Desktop
  python -m src.comment_contest --permalink https://www.instagram.com/p/SHORTCODE/ \\
      --facebook-permalink https://www.facebook.com/PageName/videos/1234567890123456/
"""
import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import anthropic
import requests
from google.cloud import bigquery

from . import bigquery_store, config
from .graph_client import InstagramGraphClient, TokenExpiredError

log = logging.getLogger(__name__)

_CLAUDE_MODEL = "claude-opus-5"
_EXTRACTION_CHUNK_SIZE = 60

_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string"},
                    "places": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Every distinct real place mentioned in this comment, exactly as "
                            "the commenter spelled/capitalized it -- don't normalize yet. Empty "
                            "list if no real place is named (jokes, 'my nonna', emojis only, "
                            "tagging a friend with no place named, etc.)."
                        ),
                    },
                },
                "required": ["comment_id", "places"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comments"],
    "additionalProperties": False,
}

_EXTRACTION_PROMPT_HEADER = (
    "These are comments on an Instagram post asking people to name their favorite "
    "tiramisu spot. For each comment, extract every distinct real place (restaurant, "
    "bakery, cafe, etc.) it actually names -- keep the commenter's own spelling and "
    "capitalization, don't normalize or correct it yet, that happens in a separate "
    "step. A comment can name zero places (e.g. \"my nonna's house\", a joke, pure "
    "emojis, tagging a friend with no place named) -- return an empty list for those. "
    "A comment can name more than one place -- list each separately, once per place. "
    "Ignore @mentions of people, unless the mention is clearly a business's own "
    "Instagram handle being given as the answer.\n\n"
    "Return exactly one entry per comment_id listed below, in the same order.\n\nComments:\n"
)

_GROUPING_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {
                        "type": "string",
                        "description": "The clearest, most complete version of this place's name.",
                    },
                    "raw_variants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Every input string that refers to this same place.",
                    },
                },
                "required": ["canonical_name", "raw_variants"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}

_GROUPING_PROMPT_HEADER = (
    "Here is a list of place names people mentioned in Instagram comments about their "
    "favorite tiramisu spot. Group any strings that clearly refer to the SAME real "
    "place -- different spelling, capitalization, abbreviation, with/without \"the\", "
    "typos, or an added location/descriptor -- under one canonical_name (pick the "
    "clearest, most complete version as the canonical name). Keep genuinely different "
    "places in separate groups, even if similar-sounding. Every string in the input "
    "list must appear in exactly one group's raw_variants.\n\nPlace names:\n"
)


def _extract_shortcode(permalink: str) -> str:
    m = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", permalink)
    if not m:
        raise ValueError(f"Couldn't find a post shortcode (the /p/XXXX/ part) in: {permalink}")
    return m.group(1)


def _resolve_post_id(bq_client, permalink: str) -> str:
    shortcode = _extract_shortcode(permalink)
    table_ref = f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET}.{bigquery_store.MASTER_TABLE}"
    query = f"""
    SELECT Post_ID, Permalink FROM `{table_ref}`
    WHERE Permalink LIKE CONCAT('%', @shortcode, '%')
    LIMIT 2
    """
    rows = list(
        bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("shortcode", "STRING", shortcode)]
            ),
        ).result()
    )
    if not rows:
        raise RuntimeError(
            f"No synced post found with shortcode '{shortcode}'. Make sure this post has "
            f"been synced (run python -m src.pipeline first), or pass --post-id directly."
        )
    if len(rows) > 1:
        raise RuntimeError(
            f"Found {len(rows)} posts matching shortcode '{shortcode}' -- pass --post-id "
            f"directly instead: {[r['Post_ID'] for r in rows]}"
        )
    log.info("Resolved %s -> Post_ID %s", permalink, rows[0]["Post_ID"])
    return rows[0]["Post_ID"]


_FACEBOOK_COMMENT_FIELDS = "id,message,from,created_time,like_count"
_FACEBOOK_MAX_RETRIES = 5
_FACEBOOK_INITIAL_BACKOFF_SECONDS = 2


def _extract_facebook_video_id(permalink: str) -> str:
    """A Page video's permalink_url embeds its numeric video ID directly
    (either /videos/<id>/ or ?v=<id>) -- unlike Instagram's opaque
    shortcode, no BigQuery lookup is needed to resolve it."""
    m = re.search(r"/videos/(\d+)", permalink) or re.search(r"[?&]v=(\d+)", permalink)
    if not m:
        raise ValueError(
            f"Couldn't find a numeric video ID in: {permalink}. This needs the canonical "
            f"facebook.com/.../videos/<id>/ (or ?v=<id>) link, not a shortened fb.watch one "
            f"-- or pass --facebook-video-id directly if you already have the raw ID."
        )
    return m.group(1)


def get_all_facebook_comments(access_token: str, video_id: str, base_url: str) -> list:
    """Every top-level comment on one Facebook Page video, via the same
    /{id}/comments edge and paging.next cursor pattern used everywhere
    else in this codebase's Graph API clients. Kept minimal/self-
    contained here rather than importing facebookpipeline's client --
    this only needs the one shared access token (see this module's
    docstring), not a second .env or Page ID."""
    next_url = f"{base_url}/{video_id}/comments"
    next_params = {"fields": _FACEBOOK_COMMENT_FIELDS, "limit": 100, "access_token": access_token}
    items = []
    while next_url:
        for attempt in range(1, _FACEBOOK_MAX_RETRIES + 1):
            resp = requests.get(next_url, params=next_params, timeout=30)
            payload = resp.json() if resp.content else {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if error is None and resp.ok:
                break
            code = error.get("code") if error else None
            message = error.get("message") if error else f"HTTP {resp.status_code}"
            if code == 190:
                raise RuntimeError(f"Facebook access token invalid/expired: {message}")
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt >= _FACEBOOK_MAX_RETRIES:
                raise RuntimeError(f"Facebook comments request failed: {message}")
            backoff = _FACEBOOK_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning("Facebook comments request failed (attempt %d/%d): %s -- retrying in %ds",
                        attempt, _FACEBOOK_MAX_RETRIES, message, backoff)
            time.sleep(backoff)
        else:
            raise RuntimeError("Exhausted retries fetching Facebook comments")

        items.extend(payload.get("data", []))
        next_url = payload.get("paging", {}).get("next")
        next_params = None

    log.info("Fetched %d Facebook comment(s) for video %s", len(items), video_id)
    return items


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _extract_places(client: anthropic.Anthropic, comments: list) -> dict:
    """comments: [{"id", "text"}, ...]. Returns {comment_id: [raw_place, ...]}."""
    results = {}
    chunks = list(_chunks(comments, _EXTRACTION_CHUNK_SIZE))
    for i, chunk in enumerate(chunks, start=1):
        log.info("Extracting places from comments %d-%d of %d...",
                  (i - 1) * _EXTRACTION_CHUNK_SIZE + 1, min(i * _EXTRACTION_CHUNK_SIZE, len(comments)), len(comments))
        lines = [f'{c["id"]}: "{(c["text"] or "").strip()}"' for c in chunk]
        prompt = _EXTRACTION_PROMPT_HEADER + "\n".join(lines)

        response = client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _EXTRACTION_SCHEMA}},
        )
        if response.stop_reason == "refusal":
            log.warning("Chunk %d refused -- treating every comment in it as no-place-mentioned.", i)
            continue
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            log.warning("Chunk %d: unparseable response, skipping: %s", i, text[:200])
            continue
        for entry in parsed.get("comments", []):
            cid = entry.get("comment_id")
            if cid:
                results[cid] = [p.strip() for p in entry.get("places", []) if p and p.strip()]
    return results


def _mechanical_key(raw: str) -> str:
    """Strips a leading '@', all whitespace/punctuation, and lowercases
    -- catches the mechanical case where the same place is typed both as
    its Instagram handle and as its plain name (e.g. "@truscottbakery"
    and "Truscott Bakery" both key to "truscottbakery"). This is a fact
    about the string, not a judgment call, so it shouldn't be left to
    the model on a list of hundreds of entries -- confirmed live that it
    otherwise missed several exactly this shape (handle vs. plain name
    for the same place counted as two different places)."""
    s = raw.strip()
    if s.startswith("@"):
        s = s[1:]
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _mechanical_precluster(raw_places: list) -> dict:
    """Returns {raw: representative} -- every raw string sharing the
    same _mechanical_key() collapses onto one representative before
    Claude ever sees the list, so the (much smaller, deduplicated) list
    handed to _group_places() only has genuine typo/spelling variants
    left for it to reason about, not mechanical @-handle duplicates.
    The representative prefers a non-'@' variant (reads better in the
    final CSV) and, among those, the longest (most likely to be the
    full written-out name rather than a shorthand)."""
    groups = defaultdict(list)
    for raw in raw_places:
        groups[_mechanical_key(raw)].append(raw)

    raw_to_representative = {}
    for key, variants in groups.items():
        if not key:
            for v in variants:
                raw_to_representative[v] = v
            continue
        non_handle = [v for v in variants if not v.strip().startswith("@")]
        pool = non_handle or variants
        # Prefer a variant with a space over a handle-styled slug (e.g.
        # "Cantina Amici" over "cantina_amici") before falling back to
        # plain length, so the representative reads like a name a human
        # typed rather than an @-handle with the @ stripped off.
        spaced = [v for v in pool if " " in v]
        pool = spaced or pool
        representative = max(pool, key=len)
        for v in variants:
            raw_to_representative[v] = representative
    return raw_to_representative


def _group_places(client: anthropic.Anthropic, raw_places: list) -> dict:
    """raw_places: distinct raw place strings. Returns {raw: canonical_name},
    case-insensitive on the input side (matching is done by exact string
    from Claude's own raw_variants, which should echo the input verbatim)."""
    if not raw_places:
        return {}

    prompt = _GROUPING_PROMPT_HEADER + "\n".join(f"- {p}" for p in raw_places)
    response = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": _GROUPING_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        log.warning("Grouping call refused -- falling back to no normalization (each raw string stands alone).")
        return {}
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("Grouping response unparseable -- falling back to no normalization.")
        return {}

    mapping = {}
    for group in parsed.get("groups", []):
        canonical = group.get("canonical_name")
        if not canonical:
            continue
        for variant in group.get("raw_variants", []):
            mapping[variant] = canonical
    return mapping


def run(
    permalink: str = None,
    post_id: str = None,
    facebook_permalink: str = None,
    facebook_video_id: str = None,
    out_dir: str = ".",
) -> int:
    if not config.ANTHROPIC_API_KEY:
        log.error("Fatal: ANTHROPIC_API_KEY is not set (see .env.example).")
        return 1

    bq_client = bigquery_store.get_client()

    if post_id:
        resolved_post_id = post_id
    else:
        resolved_post_id = _resolve_post_id(bq_client, permalink)

    graph_client = InstagramGraphClient()
    try:
        graph_client.get_account_info()
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1

    log.info("Fetching Instagram comments for Post_ID=%s ...", resolved_post_id)
    ig_comments = list(graph_client.get_all_comments(resolved_post_id))
    log.info("Fetched %d Instagram comment(s).", len(ig_comments))

    comments = [
        {"id": f"IG:{c['id']}", "text": c.get("text", ""), "username": c.get("username", ""), "platform": "Instagram"}
        for c in ig_comments
    ]

    if facebook_permalink or facebook_video_id:
        resolved_fb_video_id = facebook_video_id or _extract_facebook_video_id(facebook_permalink)
        log.info("Fetching Facebook comments for video %s ...", resolved_fb_video_id)
        fb_comments = get_all_facebook_comments(config.META_ACCESS_TOKEN, resolved_fb_video_id, config.GRAPH_BASE_URL)
        comments.extend(
            {
                "id": f"FB:{c['id']}",
                "text": c.get("message", ""),
                "username": (c.get("from") or {}).get("name", ""),
                "platform": "Facebook",
            }
            for c in fb_comments
        )

    if not comments:
        log.warning("No comments found -- nothing to do.")
        return 0
    log.info("%d comment(s) total across both platforms.", len(comments))

    anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    comment_lookup = {c["id"]: c for c in comments}
    extracted = _extract_places(
        anthropic_client, [{"id": c["id"], "text": c["text"]} for c in comments]
    )

    distinct_raw = sorted({p for places in extracted.values() for p in places})
    raw_to_mechanical_rep = _mechanical_precluster(distinct_raw)
    distinct_representatives = sorted(set(raw_to_mechanical_rep.values()))
    log.info(
        "Extracted %d place mention(s) across %d distinct raw name(s), %d after merging "
        "@handle/plain-name duplicates. Normalizing remaining spelling variants...",
        sum(len(p) for p in extracted.values()), len(distinct_raw), len(distinct_representatives),
    )
    representative_to_canonical = _group_places(anthropic_client, distinct_representatives)
    raw_to_canonical = {
        raw: representative_to_canonical.get(rep, rep) for raw, rep in raw_to_mechanical_rep.items()
    }

    out_path = Path(out_dir).expanduser()
    out_path.mkdir(parents=True, exist_ok=True)
    slug = resolved_post_id

    vote_rows = []
    no_place_rows = []
    vote_counts = defaultdict(int)
    vote_counts_by_platform = defaultdict(lambda: defaultdict(int))
    vote_examples = defaultdict(set)

    for comment_id, places in extracted.items():
        c = comment_lookup.get(comment_id, {})
        if not places:
            no_place_rows.append({
                "Comment_ID": comment_id,
                "Platform": c.get("platform", ""),
                "Username": c.get("username", ""),
                "Comment_Text": c.get("text", ""),
            })
            continue
        for raw_place in places:
            canonical = raw_to_canonical.get(raw_place, raw_place)
            vote_rows.append({
                "Comment_ID": comment_id,
                "Platform": c.get("platform", ""),
                "Username": c.get("username", ""),
                "Comment_Text": c.get("text", ""),
                "Extracted_Place_Raw": raw_place,
                "Canonical_Place": canonical,
            })
            vote_counts[canonical] += 1
            vote_counts_by_platform[canonical][c.get("platform", "")] += 1
            vote_examples[canonical].add(raw_place)

    raw_votes_path = out_path / f"{slug}_raw_votes.csv"
    with open(raw_votes_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["Comment_ID", "Platform", "Username", "Comment_Text", "Extracted_Place_Raw", "Canonical_Place"],
        )
        writer.writeheader()
        writer.writerows(vote_rows)

    summary_path = out_path / f"{slug}_summary.csv"
    ranked = sorted(vote_counts.items(), key=lambda kv: -kv[1])
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Rank", "Place", "Votes", "Instagram_Votes", "Facebook_Votes", "Raw_Spelling_Variants"]
        )
        writer.writeheader()
        for rank, (place, count) in enumerate(ranked, start=1):
            writer.writerow({
                "Rank": rank,
                "Place": place,
                "Votes": count,
                "Instagram_Votes": vote_counts_by_platform[place].get("Instagram", 0),
                "Facebook_Votes": vote_counts_by_platform[place].get("Facebook", 0),
                "Raw_Spelling_Variants": " | ".join(sorted(vote_examples[place])),
            })

    no_place_path = out_path / f"{slug}_no_place_mentioned.csv"
    with open(no_place_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Comment_ID", "Platform", "Username", "Comment_Text"])
        writer.writeheader()
        writer.writerows(no_place_rows)

    log.info(
        "Done: %d place vote(s) across %d place(s) (%d raw comment(s) had no place mentioned). "
        "Wrote:\n  %s\n  %s\n  %s",
        len(vote_rows), len(vote_counts), len(no_place_rows),
        raw_votes_path, summary_path, no_place_path,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--permalink", help="The post's Instagram URL, e.g. https://www.instagram.com/p/SHORTCODE/")
    group.add_argument("--post-id", help="The post's Post_ID directly (skips the permalink lookup).")
    fb_group = parser.add_mutually_exclusive_group()
    fb_group.add_argument(
        "--facebook-permalink",
        help="Optional: the same content's cross-posted Facebook video URL, e.g. "
        "https://www.facebook.com/PageName/videos/1234567890123456/ -- its comments get folded "
        "into the same tally.",
    )
    fb_group.add_argument("--facebook-video-id", help="The Facebook video's numeric ID directly.")
    parser.add_argument("--out-dir", default=".", help="Directory to write the CSV files into.")
    args = parser.parse_args()
    sys.exit(run(
        permalink=args.permalink,
        post_id=args.post_id,
        facebook_permalink=args.facebook_permalink,
        facebook_video_id=args.facebook_video_id,
        out_dir=args.out_dir,
    ))

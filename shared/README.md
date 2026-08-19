# Shared cross-platform content layer

The piece that ties `instagramanalyticspipeline`, `facebookpipeline`, and
(coming) `youtubepipeline` / `tiktokpipeline` together: every platform
pipeline ingests its own detailed data into its own BigQuery tables as
before, and additionally mirrors a normalized subset into this shared
layer so the same piece of content can be matched across platforms and
reported on per-partnership in one place.

## Why a separate layer instead of one big table

Each platform's data is genuinely different (Instagram has Reels-specific
watch-time metrics, Facebook exposes video length, YouTube/TikTok will
have their own quirks) -- forcing them into one schema would mean losing
detail or drowning it in nullable platform-specific columns. Instead:

- `instagram_master`, `facebook_master`, etc. stay exactly as detailed as
  each platform's pipeline already made them -- nothing about the
  existing Instagram pipeline had to change shape.
- `content_items` (this layer) carries only the normalized subset every
  platform has in common: caption, publish date, permalink, and the
  metrics that map cleanly across platforms (views/likes/comments/
  shares/saves). This is what matching and partner reporting run against.

## The three tables

- **content_items** -- one row per `{platform}:{platform_post_id}`.
  Upserted by each platform pipeline after its own ingest.
- **content_groups** -- one row per real-world piece of content (e.g. "the
  espresso machine unboxing"). `Partnership` and `Content_Type` live
  *here*, not on individual content_items, since the whole point is
  reporting on one partnership across every platform it was posted to.
- **content_group_members** -- which content_items belong to which group,
  and whether the link was auto-suggested (`Confirmed=False`, pending
  human review) or confirmed (manually, or auto-matched with high enough
  confidence -- see `src/matching.py`).

## Auto-matching

`src/matching.py` is pure Python (no BigQuery dependency, see
`tests/test_matching.py`) and scores how likely two content_items from
*different* platforms are the same underlying video, using caption
word-overlap plus how close together they were posted. High-confidence
matches (`AUTO_CONFIRM_SCORE`, currently 0.82) become a confirmed group
immediately; anything above `MIN_SUGGEST_SCORE` (0.55) but below that
becomes a pending group a human reviews.

This runs automatically at the end of every platform pipeline's run (see
`_sync_to_shared_content_layer` in each pipeline's `pipeline.py`), or
stand-alone:

```bash
python -m shared.src.run_matching
```

Nothing here is ever final -- `content_store.remove_member` /
`add_members` let a human correct a wrong auto-match or link things
manually, regardless of what the heuristic decided.

## Setup

```bash
pip install -r requirements.txt
```

Uses the same `BQ_PROJECT_ID` as every platform pipeline, plus
`SHARED_BQ_DATASET` (defaults to `social_analytics`) -- separate from each
platform's own dataset.

## Project layout

```
src/
  config.py         env-driven configuration
  content_store.py  BigQuery schema + upsert/query logic for the 3 tables above
  matching.py        pure-Python matching heuristic (no BigQuery dependency)
  run_matching.py     stand-alone entrypoint: match whatever's currently ungrouped
sql/unified_content_schema.sql   reference DDL
tests/test_matching.py            unit tests for the matching heuristic
```

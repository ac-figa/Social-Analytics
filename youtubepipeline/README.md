# Social-Analytics: YouTube

Pulls YouTube Shorts/video data + stats from the YouTube Data API v3 into
BigQuery, keyed by Video_ID, with manual `Partnership` / `Content_Type`
classifications that survive every refresh.

Same architecture as `instagramanalyticspipeline` and `facebookpipeline`,
but simpler: a plain API key against public data, no OAuth, no per-video
permission quirks -- video stats (views, likes, comments) come back
directly from the same call that lists videos, no separate insights
endpoint. Mirrors every video into the shared cross-platform
`content_items` table (see `../shared/`) so it can be automatically
matched with the same content posted to Instagram, Facebook, or TikTok.

## First-time setup

1. Get API access: `docs/SETUP.md` -- short, no app review needed.
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill it in.
4. `gcloud auth application-default login` (for local runs).

## Running the first import

```bash
python -m src.pipeline
```

## Classifying a video

```bash
python -m src.classify --video-id dQw4w9WgXcQ \
  --partnership "Caffe Borbone" --content-type "Sponsored Integration"
```

## Refreshing

```bash
python -m src.pipeline
```

Only videos published within the last `INSIGHTS_REFRESH_DAYS` (default
45) get their stats re-fetched each run -- see
`instagramanalyticspipeline/README.md`'s "Refreshing" section, identical
behavior here. For a one-off full backfill:

```bash
python -m src.pipeline --full
```

## Project layout

```
src/
  config.py          env-driven configuration
  youtube_client.py  YouTube Data API v3 client (channel/video listing, stats)
  transform.py       raw API JSON -> YouTube_Master row schema
  suggestions.py     Suggested_Partnership heuristic (shares brand_keywords.json)
  bigquery_store.py  BigQuery schema + MERGE upsert logic
  pipeline.py         main entrypoint
  classify.py         CLI to set a manual classification
sql/schema.sql        reference DDL
docs/SETUP.md          auth setup (API key, no OAuth needed)
```

# Social-Analytics: TikTok

Pulls TikTok video data + stats from the TikTok API v2 (Display API) into
BigQuery, keyed by Video_ID, with manual `Partnership` / `Content_Type`
classifications that survive every refresh.

Same architecture as the other pipelines (staging+MERGE upsert,
classification reattachment, shared cross-platform content sync), but
with a genuinely different auth model: TikTok requires real OAuth login
by the account owner (no permanent app-level token like Meta, no simple
API key like YouTube), and the resulting access token expires every 24
hours. `src/tiktok_client.py` handles re-authenticating on every run
using a stored refresh token, and automatically persists a rotated
refresh token back to `.env` when TikTok issues one -- see
`docs/SETUP.md` for the full one-time login process and why it's more
involved than the other three platforms.

`video/list/` already returns full stats (views, likes, comments,
shares) in the same call that lists videos -- no separate insights
endpoint, no permission quirks to work around like Facebook's
`video_insights`. TikTok is also the only platform that reliably exposes
a real `Shares` count.

## First-time setup

1. Get API access: `docs/SETUP.md` -- the most involved of the four
   platforms (OAuth, app review, Sandbox mode), but only needs doing once.
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill it in.
4. `gcloud auth application-default login` (for local runs).

## Running the first import

```bash
python -m src.pipeline
```

## Classifying a video

```bash
python -m src.classify --video-id 7676513953853263111 \
  --partnership "Caffe Borbone" --content-type "Sponsored Integration"
```

## Refreshing

```bash
python -m src.pipeline
```

Safe to run repeatedly. Unlike the other pipelines, there's no
`INSIGHTS_REFRESH_DAYS` window or `--full` flag here -- `video/list/`
already returns full stats for every video in one cheap call, so every
run re-syncs everything at the same cost; there's no expensive per-video
detail/insights fetch to selectively skip.

## Project layout

```
src/
  config.py          env-driven configuration + refresh-token persistence
  tiktok_client.py   TikTok API v2 client (OAuth refresh, video listing+stats)
  transform.py       raw API JSON -> TikTok_Master row schema
  suggestions.py     Suggested_Partnership heuristic (shares brand_keywords.json)
  bigquery_store.py  BigQuery schema + MERGE upsert logic
  pipeline.py         main entrypoint
  classify.py         CLI to set a manual classification
sql/schema.sql        reference DDL
docs/SETUP.md          the full OAuth/app-review/Sandbox setup process
```

## Deployment

No `Dockerfile`/`deploy/` here yet, unlike the other pipelines --
deliberately. Cloud Run Jobs are stateless (a fresh container every run),
but this pipeline needs to persist a rotated refresh token *between*
runs, and `config.py`'s current approach (writing to a local `.env` file)
only works for a long-lived local machine, not an ephemeral container.
Deploying this for unattended scheduled runs needs the refresh-token
persistence rewired to something durable across container restarts (e.g.
Secret Manager, updated via the API after each rotation) before a
Cloud Run Job setup makes sense -- not yet built.


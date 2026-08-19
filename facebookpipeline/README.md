# Social-Analytics: Facebook

Pulls Facebook Page video/Reel data + insights from the Meta Graph API
into BigQuery, keyed by Video_ID, with manual `Partnership` /
`Content_Type` classifications that survive every refresh.

Same architecture as `instagramanalyticspipeline` -- reuses the same Meta
App and access token (see `docs/SETUP.md`), and additionally mirrors
every video into the shared cross-platform `content_items` table (see
`../shared/`) so it can be automatically matched with the same content
posted to Instagram, YouTube, or TikTok.

## First-time setup

1. Get API access: `docs/SETUP.md` -- short, since it reuses the
   Instagram pipeline's Meta App/token.
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill it in.
4. `gcloud auth application-default login` (for local runs).

## Running the first import

```bash
python -m src.pipeline
```

## Classifying a video

```bash
python -m src.classify --video-id 1234567890 \
  --partnership "Caffe Borbone" --content-type "Sponsored Integration"
```

## Refreshing

```bash
python -m src.pipeline
```

Safe to run repeatedly -- see `instagramanalyticspipeline/README.md`'s
"Refreshing" section, identical behavior here.

## Project layout

```
src/
  config.py          env-driven configuration
  graph_client.py    Meta Facebook Graph API client (Page videos, insights)
  transform.py       raw API JSON -> Facebook_Master row schema
  suggestions.py     Suggested_Partnership heuristic (shares brand_keywords.json)
  bigquery_store.py  BigQuery schema + MERGE upsert logic
  pipeline.py         main entrypoint
  classify.py         CLI to set a manual classification
sql/schema.sql        reference DDL
docs/SETUP.md          auth setup (reuses the Instagram pipeline's Meta App)
deploy/README.md        Cloud Run Job + Scheduler deployment
```

# Social-Analytics

Pulls Instagram Reel (and other media) data + insights from the Meta
Instagram Graph API into BigQuery, keyed by Post_ID, with manual
`Partnership` / `Content_Type` classifications that survive every refresh.

See `docs/API_NOTES.md` for exactly which metrics/fields are obtainable
from the current API and which aren't (duration, tagged accounts, and
paid-partnership status are **not** exposed by Meta on read -- see that
doc for why).

## Architecture

```
Meta Instagram Graph API
        |
        |  paginated media list, batched detail + insights calls,
        |  retry/backoff, per-post error isolation
        v
  src/graph_client.py
        |
        v
  src/transform.py  -->  one row per post, normalized to the Instagram_Master schema
        |
        v
  src/suggestions.py --> Suggested_Partnership (caption/mention heuristic, non-authoritative)
        |
        v
  src/pipeline.py
        |  1. reads instagram_classifications into memory (Post_ID -> Partnership, Content_Type)
        |  2. reattaches those values onto every row
        |  3. MERGEs into Instagram_Master (staging-table + MERGE upsert)
        |  4. marks posts no longer returned by the API as API_Status = Deleted_or_Unavailable
        |  5. appends one deduped snapshot row per post to Instagram_Insights_History
        v
  BigQuery: instagram_master, instagram_classifications, instagram_insights_history
        |
        v
  Looker Studio / BigQuery SQL
```

`instagram_classifications` is the durable source of truth for manual
classification -- `Instagram_Master.Partnership` / `.Content_Type` are
always *derived* from it on refresh, never edited independently, so a
refresh can never silently lose a classification.

## First-time setup

1. Get API access: follow `docs/SETUP.md` end-to-end (Meta App, System
   User token, Instagram Business Account ID).
2. Get a GCP project with the BigQuery API enabled.
3. `pip install -r requirements.txt`
4. `cp .env.example .env` and fill it in.
5. `gcloud auth application-default login` (for local runs; not needed for
   the Cloud Run Job deployment -- see `deploy/README.md`).

## Running the first import

```bash
python -m src.pipeline
```

This creates the `instagram_analytics` dataset and all three tables if
they don't exist yet, pulls every media item on the account, and upserts
them into `Instagram_Master`. Every post starts with
`Partnership = Unclassified` and `Content_Type = Unclassified` until you
classify it.

Check the log output at the end of the run for a summary line and any
`Failed Post_IDs` -- a handful of failures on one run (a deleted post, a
metric Meta won't return for that media) is normal and won't block the
rest of the data from loading.

## Classifying a Reel

```bash
python -m src.classify --post-id 17895xxxxxxxxxxx \
  --partnership "Caffe Borbone" --content-type "Sponsored Integration"
```

This writes to `instagram_classifications` only. Run the pipeline again
(or just query `instagram_master` -- it's already reflected there from the
last run's reattachment, or will be on the next run) to see it applied. If
you'd rather edit classifications in bulk, `instagram_classifications` is a
plain BigQuery table -- edit it directly in the BigQuery console, or via a
connected Google Sheet, whichever fits your workflow. The next pipeline
run always reads whatever is in that table.

Check `instagram_master.Suggested_Partnership` for auto-detected hints
(from captions/@mentions/collaborators, matched against
`brand_keywords.json`) before classifying manually -- it's a hint only and
is never written to `Partnership` automatically.

## Refreshing

Same command, safe to run repeatedly:

```bash
python -m src.pipeline
```

Each run: pulls the latest API data, updates every API-derived column,
reattaches existing classifications, adds any new posts as `Unclassified`,
marks any post no longer returned by the API (without deleting its row or
history), and appends today's insights snapshot (deduped, so re-running
the same day doesn't create duplicate snapshot rows).

Only posts published within the last `INSIGHTS_REFRESH_DAYS` (default 45)
actually get their details/insights re-fetched -- older posts are still
listed every run (so nothing gets wrongly marked deleted) but their
`Instagram_Master` row is left as whatever it was on its last real sync,
since older content's numbers rarely move enough to be worth the extra API
calls.

For a one-off full backfill (refresh every post regardless of age),
without changing `.env`:

```bash
python -m src.pipeline --full
```

For unattended scheduled refreshes, see `deploy/README.md` (Cloud Run Job
+ Cloud Scheduler).

## Project layout

```
src/
  config.py          env-driven configuration
  graph_client.py    Meta Instagram Graph API client (pagination, batching, retries)
  transform.py       raw API JSON -> Instagram_Master row schema
  suggestions.py     Suggested_Partnership heuristic
  bigquery_store.py  BigQuery schema + MERGE upsert logic
  pipeline.py         main entrypoint
  classify.py         CLI to set a manual classification
sql/schema.sql        reference DDL (pipeline creates tables itself)
docs/SETUP.md          Meta App / access token walkthrough
docs/API_NOTES.md       API capabilities, limitations, metric conventions
deploy/README.md        Cloud Run Job + Scheduler deployment
brand_keywords.json     editable @mention/hashtag -> brand name map
```

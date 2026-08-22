# Authentication setup

Much simpler than the Meta pipelines -- no OAuth, no System User, no app
review. YouTube video stats (views, likes, comments) are public data, so
a plain API key is enough.

## 1. Enable the YouTube Data API

1. In Google Cloud Console, select the same project you use for BigQuery
   (`BQ_PROJECT_ID`) -- no need for a separate project.
2. Search "YouTube Data API v3" in the top search bar and click **Enable**.

## 2. Create an API key

1. **APIs & Services -> Credentials -> + Create Credentials -> API key**
2. Copy the generated key.
3. Click **Restrict key**: under "API restrictions" select **Restrict
   key** and check only **YouTube Data API v3**. Leave "Application
   restrictions" as **None** -- this key is used by a server-side script,
   not a browser or app, so IP/website restrictions don't apply cleanly
   and the API-level restriction is what actually matters for security.

This is your `YOUTUBE_API_KEY`.

## 3. Find your Channel ID

Easiest way: query the API directly with your new key and your channel's
`@handle` (replace `<HANDLE>` with yours, no `@`):

```bash
curl -s "https://www.googleapis.com/youtube/v3/channels?part=id,snippet,statistics&forHandle=<HANDLE>&key=<YOUTUBE_API_KEY>"
```

The `id` field in the response (looks like `UCxxxxxxxxxxxxxxxxxxxxxx`) is
your `YOUTUBE_CHANNEL_ID`.

## 4. Fill in `.env`

```bash
cp .env.example .env
```

Fill in `YOUTUBE_API_KEY`, `YOUTUBE_CHANNEL_ID`, `BQ_PROJECT_ID`, and
`BQ_DATASET` (defaults to `youtube_analytics`).

## Quota

The YouTube Data API uses a daily quota (10,000 units by default, free).
This pipeline only uses cheap endpoints (`playlistItems.list` and
`videos.list`, 1 unit each per page of up to 50 items) -- even a full
`--full` backfill of several hundred videos costs well under 50 units
total. If you ever see a `quotaExceeded` error, it's almost certainly
from something else using the same API key/project, not this pipeline.

## Troubleshooting

| Error | Meaning | Fix |
|---|---|---|
| `keyInvalid` / `forbidden` | Key wrong, or YouTube Data API v3 not enabled/restricted incorrectly | Re-check Steps 1-2 |
| `quotaExceeded` / `dailyLimitExceeded` | Daily quota used up (see above) | Wait for daily reset (midnight Pacific time), or request a quota increase in Cloud Console |
| Channel not found | Wrong `YOUTUBE_CHANNEL_ID` | Re-run the Step 3 lookup and double-check the `id` field |

## Notes on data

- **Duration**: unlike Instagram (never exposed) and Facebook (needs the
  Page Post Insights workaround), YouTube's API exposes video duration
  directly and reliably (ISO 8601 format, e.g. `PT43S` -- parsed to
  seconds in `src/transform.py`).
- **Shares**: not exposed by the YouTube Data API at all. Always `NULL`,
  same as Facebook.
- **Post_Type** (`Short` vs `Video`): YouTube's API doesn't label Shorts
  as a distinct type, so this is a best-effort heuristic (<=60 seconds,
  Shorts' original length limit) -- not authoritative, since Shorts can
  now run longer.

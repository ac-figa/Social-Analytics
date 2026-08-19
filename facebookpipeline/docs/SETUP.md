# Authentication setup

This pipeline reuses the **same Meta App and System User token** as
`instagramanalyticspipeline` -- Facebook Page video access is part of the
same Meta Business permission grant. If you already have Instagram
ingestion working, you likely only need one new value: `FB_PAGE_ID`.

## If you haven't set up the Instagram pipeline yet

Follow `instagramanalyticspipeline/docs/SETUP.md` Steps 1-2 first (create
the Meta App, create the System User, generate its token) -- do that once,
not per platform.

## 1. Confirm the System User has Page access

In `instagramanalyticspipeline/docs/SETUP.md` Step 2, the System User was
assigned the Facebook Page linked to your Instagram account. That's the
same Page this pipeline reads from -- no separate asset assignment needed.

Permissions: the token needs `pages_read_engagement` and
`pages_show_list`, both already requested in the Instagram pipeline's
System User token generation step. If your token predates that step, or
you scoped it down, regenerate it with those included.

## 2. Find your Facebook Page ID

You already looked this up once for the Instagram pipeline (Step 3 there):

```bash
curl -s "https://graph.facebook.com/v21.0/me/accounts?access_token=<TOKEN>"
```

The `id` field of your Page in that response is your `FB_PAGE_ID`.

## 3. Verify access end-to-end

```bash
curl -s "https://graph.facebook.com/v21.0/<FB_PAGE_ID>?fields=id,name&access_token=<TOKEN>"
```

You should get back your Page name.

## 4. Fill in `.env`

```bash
cp .env.example .env
```

Fill in `META_ACCESS_TOKEN` (same value as the Instagram pipeline's),
`FB_PAGE_ID`, `BQ_PROJECT_ID`, and `BQ_DATASET` (defaults to
`facebook_analytics`).

## Troubleshooting

Same Graph API, same error codes -- see
`instagramanalyticspipeline/docs/SETUP.md`'s Troubleshooting table.

One Facebook-specific note: if `total_video_views` or other
`video_insights` metrics start erroring for every video, Meta has likely
renamed/retired that metric on this API version -- check the current
[Video Insights reference](https://developers.facebook.com/docs/graph-api/reference/video/video_insights/)
and update `VIDEO_INSIGHTS_METRICS` in `src/graph_client.py`.

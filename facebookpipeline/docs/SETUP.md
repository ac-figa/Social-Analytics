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

### `Views_Organic`, `Impressions`, `Average_Watch_Time`, `Watch_Time` are always NULL

This isn't a bug to fix on your end -- confirmed live (Aug 2026) that
Meta's `/video_insights` endpoint returns a successful response with
every metric hardcoded to `0` for this Page, regardless of a video's real
engagement, across both regular videos and Reels. Not a permission issue
(`read_insights` present and confirmed working elsewhere), not
content-type-specific -- the endpoint itself appears non-functional for
this Page, likely tied to Meta's "new Pages experience" migration (the
same underlying quirk that requires the Page Access Token exchange in
`get_page_info()`).

Given the API confidently returns `0` rather than erroring, there's no
reliable way for the pipeline to distinguish "genuinely zero engagement"
from "this metric doesn't work for this Page" -- so `src/graph_client.py`
doesn't call `/video_insights` at all anymore. `Views` instead comes from
the video object's own `views` field, which does return real numbers;
the other four columns stay `NULL` on purpose (honest "unavailable"
rather than a fake zero) until Meta fixes this for Pages like this one.

If you want to re-check whether it's fixed, test directly:

```bash
curl -s "https://graph.facebook.com/v21.0/<VIDEO_ID>/video_insights?metric=total_video_views&access_token=<PAGE_TOKEN>"
```

(Note: this needs a Page Access Token, not the System User token directly
-- see the Page Access Token note above for how to get one.) If it
returns a real non-zero value, `get_video_details`/`build_master_row` in
`src/graph_client.py` / `src/transform.py` can be extended to use it
again.

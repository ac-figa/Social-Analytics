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

### Views/Views_Organic/Average_Watch_Time/Watch_Time come from Page Post Insights, not video_insights

Confirmed live (Aug 2026): `/{video-id}/video_insights` returns a
successful response with every metric hardcoded to `0` for this Page,
regardless of a video's real engagement, across both regular videos and
Reels. Not a permission issue (`read_insights` present and confirmed
working elsewhere), not content-type-specific -- the endpoint itself
appears non-functional for this Page, likely tied to Meta's "new Pages
experience" migration (the same underlying quirk that requires the Page
Access Token exchange in `get_page_info()`).

The same metrics *do* work, just via a different, older API: every video
has a distinct Page Post ID (`views_detail["post_id"]`, different from
the video ID), and `/{page-id}_{post-id}/insights` -- not
`/{video-id}/video_insights` -- returns real numbers for
`post_video_views`, `post_video_views_organic`,
`post_video_avg_time_watched`, and `post_video_view_time`. That's what
`get_post_insights()` in `src/graph_client.py` calls. `Views` prefers
this insights value and only falls back to the video object's own
(differently-defined, less standard) `views` field if a video has no
`post_id` or its insights call fails outright.

`Impressions` and `Shares` genuinely have no working metric as of this
writing -- every impressions-metric name tried (`post_impressions`,
`post_impressions_unique`, `post_impressions_organic`) returns "not a
valid insights metric," and the post object's `shares` field simply isn't
returned at all. Meta appears to have deprecated Page post impressions
entirely, mirroring how it deprecated Instagram's own `impressions`
metric (see `instagramanalyticspipeline/docs/API_NOTES.md`). These two
stay `NULL` -- honest "unavailable" rather than a fake zero.

If you want to re-check any of this later, test directly against a video
with known real engagement:

```bash
# Get the video's post_id:
curl -s "https://graph.facebook.com/v21.0/<VIDEO_ID>?fields=post_id&access_token=<PAGE_TOKEN>"
# Then query Page Post Insights on the composite ID:
curl -s "https://graph.facebook.com/v21.0/<PAGE_ID>_<POST_ID>/insights?metric=post_video_views&access_token=<PAGE_TOKEN>"
```

(Needs a Page Access Token, not the System User token directly -- see the
Page Access Token note above for how to get one.)

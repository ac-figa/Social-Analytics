# Meta / Instagram Graph API research notes

Researched against current (2026) Meta developer documentation. Re-check
this file whenever `GRAPH_API_VERSION` is bumped -- Meta has deprecated
Instagram Insights metrics with little notice before (see Jan 2025 wave
below).

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Account identity | `GET /{ig-user-id}?fields=id,username,name` |
| List all media (paginated) | `GET /{ig-user-id}/media?fields=id,media_product_type,timestamp` |
| Media details | `GET /{ig-media-id}?fields=...` (batched, 50/request) |
| Media insights | `GET /{ig-media-id}/insights?metric=...` (batched, 50/request) |
| Collaborators (own posts only) | `GET /{ig-media-id}/collaborators` |

## Media fields used

`id, caption, media_type, media_product_type, timestamp, permalink, username,
owner, like_count, comments_count, is_shared_to_feed, media_audio_type,
shortcode`

`media_product_type = REELS` is how Reels are identified among all media.

## Insights metrics used

**Reels** (`media_product_type = REELS`):
`views, reach, saved, shares, total_interactions, ig_reels_avg_watch_time,
ig_reels_video_view_total_time, follows`

**Everything else** (feed video/image/carousel) -- a smaller, broadly-supported
set, since Reels-only metrics reliably error on non-Reels media:
`reach, saved, shares, total_interactions`

`follows` was in this list originally but turned out to reliably error
with `(#100) The Media Insights API does not support the follows metric
for this media product type` on live non-Reels posts, despite Meta's docs
implying it was broadly supported -- confirmed against a real account in
Aug 2026. Removed from `OTHER_INSIGHTS_METRICS`; still requested for
Reels, where it does work.

`likes` and `comments` are read from the media object's `like_count` /
`comments_count` fields instead of `/insights`, since they're available
there directly and save an API call.

### Deprecated / retired
- `impressions` -- deprecated for any media created after **July 2,
  2024**. Still queryable for older posts; the pipeline doesn't request it
  at all, since "deprecated for new posts only" is exactly the kind of
  inconsistent-per-post behavior that isn't worth the complexity.
- `video_views` / `plays` -- folded into the unified `views` metric.
- A broader deprecation wave hit Jan 8, 2025 (Graph API v21): `email_contacts`
  time series, `profile_views`, `website_clicks`, `phone_call_clicks`,
  `text_message_clicks` were removed. None of these were Reel metrics we use,
  but if you extend this pipeline to account-level insights, don't reach for
  them.

### Lower-confidence metrics (not requested by default)
`crossposted_views`, `facebook_views`, `reposts`, `reels_skip_rate` appear in
some current documentation but weren't confirmed against a live response for
this account. If you want them, add them to `REELS_INSIGHTS_METRICS` in
`src/graph_client.py` -- the per-metric fallback path will cleanly degrade to
`None` if your account/media doesn't support one, rather than failing the
whole insights call.

## Confirmed gaps -- cannot be filled from the API

| Field the business wants | Status |
|---|---|
| **Duration** | Not exposed on read for any media type. `Instagram_Master.Duration` is always `NULL`. |
| **Tagged accounts** | No field returns "who is tagged in this post." The `/{ig-user-id}/tags` edge is the *reverse* (posts where your account was tagged by someone else) and isn't useful here. `Tagged` stays `NULL`; caption `@mentions` are used only as a heuristic input to `Suggested_Partnership`, not as a real tag list. |
| **Collaborators** | Exposed via the `collaborators` edge, but only for media where your account is the original publisher (true for everything this pipeline pulls) and only lists actual IG co-author collaborators -- not general "featuring a brand." Used for `Collabed` / `Collaborator_Usernames`. |
| **Paid partnership / branded content label** | Not exposed as a readable field on existing organic media. Meta added support for setting this label via the *Content Publishing API* (write-side, for content published through the API), but there is no confirmed GET field for reading the disclosure status of already-published posts -- including posts published manually through the Instagram app, which is most of what this pipeline will see. This is why `Partnership` must stay a manual, human-owned classification: the API cannot be trusted as a source of truth for it, now or for the foreseeable future. |

## Metric conventions: `0` vs `null`

- **`0`** means the API was asked for that metric and explicitly returned
  a value of zero -- a real "no engagement yet" answer.
- **`null`** means we don't actually know: the metric isn't supported for
  that media type/API version, the account doesn't meet a minimum
  threshold (some engagement metrics require a minimum follower/view
  count before Meta will return real data), or the request failed even
  after the per-metric fallback retry.

Defaulting missing data to `0` would make "we couldn't retrieve this"
indistinguishable from "this Reel genuinely got zero views," which would
quietly corrupt any aggregate reporting downstream (BigQuery/Looker
Studio). Keeping `null` explicit means those rows are visibly incomplete
instead of silently wrong.

## Cumulative vs. time-series

Per-media insights (`views`, `reach`, `likes`, etc.) are **lifetime
cumulative counters** -- there's no native daily/weekly time series at the
media level the way there is for account-level insights. This is why
`Instagram_Insights_History` exists: every pipeline run appends one
snapshot row per Post_ID (deduped by `Post_ID + Snapshot_Date`), which is
the only way to later compute "views gained in the last 7 days" etc.

## Rate limits & reliability

Instagram Graph API calls are batched (up to 50 sub-requests per HTTP
call) rather than issued one-by-one, both for media details and for
insights. On a rate-limit or transient (5xx) response, the client retries
with exponential backoff (2s, 4s, 8s, 16s, 32s). A single post's insights
failing (e.g. one unsupported metric) triggers a per-metric fallback for
just that post rather than aborting the batch, and a single post's total
failure is logged with its Post_ID and skipped -- the rest of the run
still completes.

## Permissions/scopes required

`instagram_basic`, `instagram_manage_insights`, `pages_show_list`,
`pages_read_engagement`, `business_management` -- granted to a System User
in Meta Business Manager. See `docs/SETUP.md`.

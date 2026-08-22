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
`views, total_views, total_likes, total_comments, reach, saved, shares,
total_interactions, ig_reels_avg_watch_time, ig_reels_video_view_total_time,
follows`

**Everything else** (feed video/image/carousel) -- a smaller, broadly-supported
set, since Reels-only metrics reliably error on non-Reels media:
`views, total_views, total_likes, total_comments, reach, saved, shares,
total_interactions`

`follows` was in this list originally but turned out to reliably error
with `(#100) The Media Insights API does not support the follows metric
for this media product type` on live non-Reels posts, despite Meta's docs
implying it was broadly supported -- confirmed against a real account in
Aug 2026. Removed from `OTHER_INSIGHTS_METRICS`; still requested for
Reels, where it does work.

`likes` and `comments` (the *organic-only* counts) are read from the media
object's `like_count` / `comments_count` fields instead of `/insights`,
since they're available there directly and save an API call. The
paid-inclusive totals (see below) do come from `/insights`.

`views` was missing from `OTHER_INSIGHTS_METRICS` until Aug 2026 -- a
latent gap where `Instagram_Master.Views` was silently `None` for every
non-Reels post. Confirmed live that `views` is in fact a valid metric for
`FEED` media too; added.

## Boosted/paid views (Aug 2026)

`Views`/`Likes`/`Comments` as read from `views`/`like_count`/`comments_count`
are **organic only** -- they do not include reach from paid distribution,
e.g. a partner brand running **Partnership Ads** (formerly Branded Content
Ads) on this account's content via their own ad account. This was
confirmed to matter a lot in practice: spot-checking 50 recent Reels found
`total_views` exceeding `views` on 37 of them, by as much as 7.2M vs 1.4M
on one post.

Meta added `total_views`, `total_likes`, `total_comments` to the
`/{ig-media-id}/insights` endpoint specifically to solve this -- they
aggregate a post's performance "across all surfaces" (organic + any paid
placement the same content object was used in, including Partnership Ads),
confirmed live against this account's own token (Instagram API with
Facebook Login / System User, no Marketing API or ad-account access
needed). `Views`/`Likes`/`Comments` in `Instagram_Master` now prefer these
`total_*` metrics, falling back to the organic-only value only if
`total_*` wasn't returned. The organic-only value is preserved separately
as `Views_Organic` (mirroring `facebookpipeline`'s `Views`/`Views_Organic`
split) so the paid contribution can still be seen (`Views - Views_Organic`).

This does **not** require Marketing API access, `ads_read`, or the System
User being assigned to any Ad Account -- that path was explored first
(see git history) and turned out to be a dead end for this use case: the
Ad Account those permissions would unlock is *this account's own*, but the
boosted views come from the *partner's* Ad Account (Partnership Ads run by
the partner, using their own ad account, against this account's content)
-- something the Marketing API on this account's token was never going to
be able to see. `total_views` sidesteps that entirely because it's scoped
to the content object, not to any one ad account.

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
| **Duration** | Not exposed on read for any media type -- the Graph API itself never returns it, confirmed live against `duration`/`video_duration`/`media_duration` fields and the full `/insights` metric list. Not worked around by scraping either (Apify-style tools that surface an IG "duration" go through Instagram's private/internal endpoints, a ToS violation risk not worth taking against this Business Manager). `transform.py` always produces `NULL` for it. As of Aug 2026, `shared/src/backfill_instagram_duration.py` fills it in *indirectly*, for any post matched to a Facebook cross-post in `content_groups` -- the two are the same video file, so Facebook's `Length` (which the Graph API does expose) becomes Instagram's Duration. Still `NULL` for anything never cross-posted to Facebook. |
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

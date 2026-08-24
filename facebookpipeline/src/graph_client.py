"""
Thin, resilient client for the Meta Facebook Graph API (Page videos/reels).

Same design as instagramanalyticspipeline/src/graph_client.py -- both hit
the same underlying Graph API, so the batching/retry/error-handling shape
is deliberately identical:
- Video listing paginates via the standard `paging.next` cursor.
- Video details + insights are fetched via the Graph API *batch* endpoint
  (up to 50 sub-requests per HTTP call).
- A batch sub-request failing never aborts the run: failures are isolated,
  logged with the Video_ID, and retried individually.
- A token/permission failure (OAuthException, code 190) aborts the whole
  run immediately.

`/{video-id}/video_insights` (total_video_views and friends) was tried
first but confirmed live, across both regular videos and Reels, to
return a successful 200 with every metric hardcoded to 0 for this
account/Page -- not an error, not permission-related (read_insights
present and working), just silently wrong data.

The metrics that matter (views, organic views, watch time) *do* work,
just via a different ID: every video has a distinct Page Post ID
(`detail["post_id"]`), and `/{page-id}_{post-id}/insights` -- the older,
separate Page Post Insights API -- returns real numbers for
`post_video_views`, `post_video_views_organic`,
`post_video_avg_time_watched`, and `post_video_view_time`. Confirmed
against a real video with 32 likes: video-level insights reported 0
everywhere, post-level insights reported 1872 views. `post_impressions`
and every impressions-metric variant tried return "not a valid insights
metric" -- Meta appears to have deprecated Page post impressions
entirely, same as it deprecated Instagram's `impressions` metric (see
instagramanalyticspipeline/docs/API_NOTES.md) -- so `Impressions` stays
`None`, genuinely unavailable rather than unfetched. `Shares` was also
re-checked directly on the post object and simply isn't returned at all.

Some entries in `/{page-id}/videos` are not real published Page posts at
all -- confirmed live (Aug 2026) via the dashboard's Browse tab, where a
chunk of videos showed up with no caption at all. Checked directly: these
have no `description`, no `likes` object (not even `{total_count: 0}` --
the key is absent entirely, unlike `comments` which is always present),
and don't appear in the Page's own `/posts` feed under their `post_id` at
all. Not Reels either (checked `/video_reels` directly -- it only
returned the properly-published ones). Best guess: leftover/duplicate
upload artifacts, not content ever actually published to the Page.
`pipeline.py` filters these out using the missing `likes` object as the
signal (the one field a real post always has, even at zero) rather than
missing `description` (a real post could legitimately have no caption).

See docs/SETUP.md for the full writeup.
"""
import json
import logging
import time
from typing import Iterator

import requests

from . import config

log = logging.getLogger(__name__)

VIDEO_LIST_FIELDS = "id,created_time"

VIDEO_DETAIL_FIELDS = (
    "id,description,created_time,permalink_url,length,views,post_id,"
    "likes.summary(true).limit(0),comments.summary(true).limit(0)"
)

# Page Post Insights metrics -- fetched via /{page-id}_{post-id}/insights,
# NOT /video_insights (broken for this account, see module docstring).
POST_INSIGHTS_METRICS = [
    "post_video_views",
    "post_video_views_organic",
    "post_video_avg_time_watched",
    "post_video_view_time",
]

# Same rate-limit codes as the Instagram client -- shared Graph API.
RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}

BATCH_CHUNK_SIZE = 50
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2


class GraphAPIError(Exception):
    def __init__(self, message, code=None, post_id=None):
        super().__init__(message)
        self.code = code
        self.post_id = post_id


class TokenExpiredError(GraphAPIError):
    """Fatal: the access token is invalid/expired or lacks permissions."""


class FacebookGraphClient:
    def __init__(self, access_token=None, page_id=None, base_url=None):
        self.access_token = access_token or config.META_ACCESS_TOKEN
        self.page_id = page_id or config.FB_PAGE_ID
        self.base_url = base_url or config.GRAPH_BASE_URL
        self.session = requests.Session()

    # ---------------------------------------------------------------- #
    # Low-level HTTP helpers
    # ---------------------------------------------------------------- #
    def _get(self, relative_url: str, params: dict) -> dict:
        params = {**params, "access_token": self.access_token}
        url = f"{self.base_url}/{relative_url}"
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.get(url, params=params, timeout=30)
            payload = _safe_json(resp)
            error = payload.get("error") if isinstance(payload, dict) else None
            if error is None and resp.ok:
                return payload
            _raise_or_backoff(error, resp.status_code, attempt, post_id=None)
        raise GraphAPIError(f"Exhausted retries for GET {relative_url}")

    def _batch(self, batch_items: list) -> list:
        data = {
            "access_token": self.access_token,
            "batch": json.dumps(batch_items),
            "include_headers": "false",
        }
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.post(f"{self.base_url}/", data=data, timeout=60)
            if resp.ok:
                result = _safe_json(resp)
                if isinstance(result, list):
                    return result
                error = result.get("error") if isinstance(result, dict) else None
                _raise_or_backoff(error, resp.status_code, attempt, post_id=None)
            else:
                payload = _safe_json(resp)
                error = payload.get("error") if isinstance(payload, dict) else None
                _raise_or_backoff(error, resp.status_code, attempt, post_id=None)
        raise GraphAPIError("Exhausted retries for batch request")

    def get_page_info(self) -> dict:
        """Also exchanges self.access_token for a Page Access Token,
        required for every call below under Meta's "new Pages experience"
        -- the System User token alone authenticates fine but silently
        returns empty results (not an error) for /videos on some accounts,
        which is why this exchange isn't optional. Piggybacked onto this
        call's existing fields param rather than a separate request."""
        payload = self._get(self.page_id, {"fields": "id,name,username,access_token"})
        page_token = payload.get("access_token")
        if page_token:
            self.access_token = page_token
        return payload

    # ---------------------------------------------------------------- #
    # Video listing
    # ---------------------------------------------------------------- #
    def get_all_video_ids(self) -> Iterator[dict]:
        """Yields {"id": ..., "created_time": ...} for every video posted
        to the Page, following pagination cursors. Includes Reels -- on
        current Graph API versions, Page Reels appear in this same
        /{page-id}/videos listing alongside regular videos."""
        relative_url = f"{self.page_id}/videos"
        params = {"fields": VIDEO_LIST_FIELDS, "limit": 100}
        url = f"{self.base_url}/{relative_url}"
        next_url = url
        next_params = params
        seen = 0
        while next_url:
            for attempt in range(1, MAX_RETRIES + 1):
                resp = self.session.get(
                    next_url,
                    params={**next_params, "access_token": self.access_token}
                    if next_params
                    else None,
                    timeout=30,
                )
                payload = _safe_json(resp)
                error = payload.get("error") if isinstance(payload, dict) else None
                if error is None and resp.ok:
                    break
                _raise_or_backoff(error, resp.status_code, attempt, post_id=None)
            else:
                raise GraphAPIError("Exhausted retries listing videos")

            for item in payload.get("data", []):
                seen += 1
                yield item

            next_url = payload.get("paging", {}).get("next")
            next_params = None

        log.info("Listed %d videos for Page %s", seen, self.page_id)

    # ---------------------------------------------------------------- #
    # Video details (batched)
    # ---------------------------------------------------------------- #
    def get_video_details(self, video_ids: list) -> dict:
        results = {}
        for chunk in _chunks(video_ids, BATCH_CHUNK_SIZE):
            batch_items = [
                {"method": "GET", "relative_url": f"{vid}?fields={VIDEO_DETAIL_FIELDS}"}
                for vid in chunk
            ]
            responses = self._batch(batch_items)
            for vid, item in zip(chunk, responses):
                body = _safe_json_str(item.get("body"))
                if item.get("code") == 200 and "error" not in body:
                    results[vid] = body
                else:
                    error = body.get("error", {})
                    log.warning(
                        "Failed to fetch video details for %s: %s",
                        vid,
                        error.get("message", body),
                    )
        return results

    # ---------------------------------------------------------------- #
    # Post insights (batched, with per-post fallback) -- see module
    # docstring for why this is keyed by post_id, not video_id
    # ---------------------------------------------------------------- #
    def get_post_insights(self, video_details: dict) -> dict:
        """video_details: {video_id: detail_dict} from get_video_details(),
        each with a 'post_id' (absent details, e.g. failed ones, are simply
        skipped -- no insights to fetch for those anyway). Returns
        {video_id: {metric_name: value}}."""
        id_pairs = [
            (video_id, detail["post_id"])
            for video_id, detail in video_details.items()
            if detail.get("post_id")
        ]
        results = {}
        failed_post_ids = []

        for chunk in _chunks(id_pairs, BATCH_CHUNK_SIZE):
            batch_items = [
                {
                    "method": "GET",
                    "relative_url": f"{self.page_id}_{post_id}/insights?metric={','.join(POST_INSIGHTS_METRICS)}",
                }
                for _, post_id in chunk
            ]
            responses = self._batch(batch_items)
            for (video_id, post_id), resp in zip(chunk, responses):
                body = _safe_json_str(resp.get("body"))
                if resp.get("code") == 200 and "error" not in body:
                    results[video_id] = _lifetime_values(body.get("data", []))
                else:
                    failed_post_ids.append((video_id, post_id))

        for video_id, post_id in failed_post_ids:
            results[video_id] = self._fallback_post_insights_per_metric(post_id)

        return results

    def _fallback_post_insights_per_metric(self, post_id: str) -> dict:
        composite_id = f"{self.page_id}_{post_id}"
        salvaged = {}
        for metric in POST_INSIGHTS_METRICS:
            try:
                payload = self._get(f"{composite_id}/insights", {"metric": metric})
                salvaged.update(_lifetime_values(payload.get("data", [])))
            except TokenExpiredError:
                raise
            except GraphAPIError as e:
                log.warning(
                    "Post insight '%s' unavailable for post %s: %s", metric, composite_id, e
                )
                salvaged[metric] = None
        return salvaged


# ---------------------------------------------------------------------- #
# Module-level helpers (identical logic to the Instagram client -- same API)
# ---------------------------------------------------------------------- #
def _lifetime_values(metric_objs: list) -> dict:
    """Post Insights metrics come back with both a 'lifetime' and a 'day'
    period in the same response -- we only want the lifetime total."""
    return {
        m["name"]: _extract_metric_value(m) for m in metric_objs if m.get("period") == "lifetime"
    }


def _extract_metric_value(metric_obj: dict):
    if "total_value" in metric_obj:
        return metric_obj["total_value"].get("value")
    values = metric_obj.get("values")
    if values:
        return values[-1].get("value")
    return None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def _safe_json_str(body_str) -> dict:
    if body_str is None:
        return {}
    try:
        return json.loads(body_str)
    except (TypeError, ValueError):
        return {}


def _raise_or_backoff(error, status_code, attempt, post_id):
    if error is None and status_code and status_code < 500 and status_code != 429:
        raise GraphAPIError(f"HTTP {status_code} with no error body", post_id=post_id)

    code = error.get("code") if error else None
    message = error.get("message") if error else f"HTTP {status_code}"

    if code == 190:
        raise TokenExpiredError(
            f"Access token invalid/expired or missing permissions: {message}. "
            f"See docs/SETUP.md to regenerate.",
            code=code,
            post_id=post_id,
        )

    retryable = code in RATE_LIMIT_ERROR_CODES or status_code == 429 or (
        status_code and status_code >= 500
    )
    if not retryable or attempt >= MAX_RETRIES:
        raise GraphAPIError(message, code=code, post_id=post_id)

    backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
    log.warning(
        "Rate limited / transient error (code=%s, http=%s): %s -- retrying in %ds "
        "(attempt %d/%d)",
        code,
        status_code,
        message,
        backoff,
        attempt,
        MAX_RETRIES,
    )
    time.sleep(backoff)

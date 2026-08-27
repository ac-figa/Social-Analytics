"""
Thin, resilient client for the Meta Instagram Graph API.

Design notes (see docs/API_NOTES.md for the full research writeup):
- Media listing paginates via the standard `paging.next` cursor.
- Media details + insights are fetched via the Graph API *batch* endpoint
  (up to 50 sub-requests per HTTP call) to stay well under rate limits.
- A batch sub-request failing (e.g. an insight unsupported for one media
  item) never aborts the run: failures are isolated, logged with the
  Post_ID, and retried individually so partial data can still be salvaged.
- A token/permission failure (OAuthException, code 190) is NOT retried
  per-post -- it aborts the whole run immediately, since every subsequent
  call would fail identically.
"""
import json
import logging
import time
from typing import Iterator

import requests

from . import config

log = logging.getLogger(__name__)

MEDIA_LIST_FIELDS = "id,media_product_type,timestamp"

MEDIA_DETAIL_FIELDS = (
    "id,caption,media_type,media_product_type,timestamp,permalink,"
    "username,owner,like_count,comments_count,is_shared_to_feed,"
    "media_audio_type,shortcode"
)

# Reels get the full Reels-specific metric set. Other media types (feed
# video/image/carousel) only get the metrics documented as broadly
# supported, to avoid a single unsupported metric failing the whole call
# -- see docs/API_NOTES.md for which metrics are Reels-only.
#
# total_views/total_likes/total_comments are aggregated across ALL
# placements a post appears in -- including paid/boosted distribution
# (e.g. a partner running Partnership Ads on this content) -- unlike
# views/likes/comments, which are organic-only. See docs/API_NOTES.md
# "Boosted/paid views" for how this was confirmed live.
REELS_INSIGHTS_METRICS = [
    "views",
    "total_views",
    "total_likes",
    "total_comments",
    "reach",
    "saved",
    "shares",
    "total_interactions",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
    "follows",
]
OTHER_INSIGHTS_METRICS = [
    "views",
    "total_views",
    "total_likes",
    "total_comments",
    "reach",
    "saved",
    "shares",
    "total_interactions",
]

# Error codes/subcodes that mean "back off and retry", not "this data is
# unavailable". 190 (OAuthException) is deliberately excluded -- that's a
# fatal, non-retryable auth problem.
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


class RateLimitedError(GraphAPIError):
    """Fatal for this run: Meta's named app/user/page-level rate-limit
    codes (4/17/32/613) mean a throttle window that resets on the order
    of an hour, not seconds -- confirmed live (Aug 2026, same Graph API,
    facebookpipeline) that retrying with the usual short exponential
    backoff (tops out at 16s) just burns time finding nothing, for every
    one of hundreds of posts, without ever actually clearing. Raised
    immediately, no retry, so the run aborts fast with a clear message."""


class InstagramGraphClient:
    def __init__(self, access_token=None, ig_user_id=None, base_url=None):
        self.access_token = access_token or config.META_ACCESS_TOKEN
        self.ig_user_id = ig_user_id or config.IG_USER_ID
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
        """POST a Graph API batch request. Returns the raw list of
        per-item response dicts (each with code/body), in the same order
        as batch_items. Raises TokenExpiredError only if the *outer*
        request itself fails on auth -- per-item auth errors are returned
        inline and handled by the caller."""
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
                # Non-list body on a 200 means something went wrong at the
                # outer level (e.g. a top-level error object).
                error = result.get("error") if isinstance(result, dict) else None
                _raise_or_backoff(error, resp.status_code, attempt, post_id=None)
            else:
                payload = _safe_json(resp)
                error = payload.get("error") if isinstance(payload, dict) else None
                _raise_or_backoff(error, resp.status_code, attempt, post_id=None)
        raise GraphAPIError("Exhausted retries for batch request")

    def get_account_info(self) -> dict:
        return self._get(self.ig_user_id, {"fields": "id,username,name,followers_count"})

    # ---------------------------------------------------------------- #
    # Media listing
    # ---------------------------------------------------------------- #
    def get_all_media_ids(self) -> Iterator[dict]:
        """Yields {"id": ..., "media_product_type": ..., "timestamp": ...}
        for every media item on the account, following pagination cursors."""
        relative_url = f"{self.ig_user_id}/media"
        params = {"fields": MEDIA_LIST_FIELDS, "limit": 100}
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
                raise GraphAPIError("Exhausted retries listing media")

            for item in payload.get("data", []):
                seen += 1
                yield item

            next_url = payload.get("paging", {}).get("next")
            next_params = None  # cursor is baked into next_url already

        log.info("Listed %d media items for account %s", seen, self.ig_user_id)

    # ---------------------------------------------------------------- #
    # Media details (batched)
    # ---------------------------------------------------------------- #
    def get_media_details(self, media_ids: list) -> dict:
        """Returns {media_id: details_dict}. Media that fail to fetch are
        omitted and logged, not fatal to the batch."""
        results = {}
        for chunk in _chunks(media_ids, BATCH_CHUNK_SIZE):
            batch_items = [
                {"method": "GET", "relative_url": f"{mid}?fields={MEDIA_DETAIL_FIELDS}"}
                for mid in chunk
            ]
            responses = self._batch(batch_items)
            for mid, item in zip(chunk, responses):
                body = _safe_json_str(item.get("body"))
                if item.get("code") == 200 and "error" not in body:
                    results[mid] = body
                else:
                    error = body.get("error", {})
                    log.warning(
                        "Failed to fetch media details for %s: %s",
                        mid,
                        error.get("message", body),
                    )
        return results

    # ---------------------------------------------------------------- #
    # Insights (batched, with per-post fallback)
    # ---------------------------------------------------------------- #
    def get_media_insights(self, media_items: list) -> dict:
        """media_items: list of {"id": ..., "media_product_type": ...}.
        Returns {media_id: {metric: value_or_None}}. Never raises for
        per-post/per-metric failures -- those degrade to None with a
        logged reason."""
        results = {}
        failed_items = []

        for chunk in _chunks(media_items, BATCH_CHUNK_SIZE):
            batch_items = []
            for item in chunk:
                metrics = _metrics_for(item.get("media_product_type"))
                batch_items.append(
                    {
                        "method": "GET",
                        "relative_url": f"{item['id']}/insights?metric={','.join(metrics)}",
                    }
                )
            responses = self._batch(batch_items)
            for item, resp in zip(chunk, responses):
                mid = item["id"]
                body = _safe_json_str(resp.get("body"))
                if resp.get("code") == 200 and "error" not in body:
                    results[mid] = {
                        m["name"]: _extract_metric_value(m)
                        for m in body.get("data", [])
                    }
                else:
                    failed_items.append(item)

        # Fallback: salvage whatever metrics we can, one at a time, only
        # for the media whose combined-metric call failed.
        for item in failed_items:
            results[item["id"]] = self._fallback_insights_per_metric(item)

        return results

    def _fallback_insights_per_metric(self, item: dict) -> dict:
        mid = item["id"]
        metrics = _metrics_for(item.get("media_product_type"))
        salvaged = {}
        for metric in metrics:
            try:
                payload = self._get(f"{mid}/insights", {"metric": metric})
                data = payload.get("data", [])
                salvaged[metric] = _extract_metric_value(data[0]) if data else None
            except (TokenExpiredError, RateLimitedError):
                raise
            except GraphAPIError as e:
                log.warning(
                    "Insight '%s' unavailable for Post_ID=%s: %s", metric, mid, e
                )
                salvaged[metric] = None
        return salvaged

    # ---------------------------------------------------------------- #
    # Collaborators (best-effort, own posts only)
    # ---------------------------------------------------------------- #
    def get_collaborators(self, media_id: str) -> list:
        try:
            payload = self._get(f"{media_id}/collaborators", {})
            return [u.get("username") for u in payload.get("data", []) if u.get("username")]
        except (TokenExpiredError, RateLimitedError):
            raise
        except GraphAPIError as e:
            log.debug("No collaborator data for Post_ID=%s: %s", media_id, e)
            return []


# ---------------------------------------------------------------------- #
# Module-level helpers
# ---------------------------------------------------------------------- #
def _metrics_for(media_product_type: str) -> list:
    if media_product_type == "REELS":
        return REELS_INSIGHTS_METRICS
    return OTHER_INSIGHTS_METRICS


def _extract_metric_value(metric_obj: dict):
    """IG insight metrics are typically a single lifetime total_value,
    occasionally a values[] time series. Prefer total_value; fall back to
    the last values[] entry; else None."""
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
    """Given a Graph API error object (or None) and the HTTP status,
    either raise immediately (fatal) or sleep and allow the caller's loop
    to retry."""
    if error is None and status_code and status_code < 500 and status_code != 429:
        # No error object but also not clearly retryable -- surface it.
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

    if code in RATE_LIMIT_ERROR_CODES:
        raise RateLimitedError(
            f"Meta rate limit hit (code={code}): {message}. This is an app/user/page-level "
            f"throttle that resets in roughly an hour, not something a short retry clears -- "
            f"wait and re-run rather than retrying immediately.",
            code=code,
            post_id=post_id,
        )

    retryable = status_code == 429 or (status_code and status_code >= 500)
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

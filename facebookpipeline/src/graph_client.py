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

`/video_insights` (total_video_views and friends) was tried first but
confirmed live, across both regular videos and Reels, to return a
successful 200 with every metric hardcoded to 0 for this account/Page --
not an error, not permission-related (read_insights present and working),
just silently wrong data. The plain `views` field on the video object
itself is the one source that returns real numbers, so that's what Views
is read from; the other video-level metrics (organic views, impressions,
watch time) have no equivalent object field and are left `None` rather
than reported as a fake zero. See docs/SETUP.md for the full writeup --
worth re-testing `/video_insights` if Meta ever fixes this for "new Pages
experience" Pages.
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
    "id,description,created_time,permalink_url,length,views,"
    "likes.summary(true).limit(0),comments.summary(true).limit(0)"
)

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


# ---------------------------------------------------------------------- #
# Module-level helpers (identical logic to the Instagram client -- same API)
# ---------------------------------------------------------------------- #
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

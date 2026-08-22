"""
Thin, resilient client for the YouTube Data API v3.

Much simpler than the Meta clients: authenticated with a single API key
against public data (no OAuth, no per-video permission quirks), and
/videos accepts up to 50 comma-separated IDs per call with stats
returned directly in the same response -- no separate insights/batch
endpoint needed, and no equivalent of Facebook's video_insights/
read_insights saga.

- Video listing paginates through the channel's uploads playlist via the
  standard nextPageToken cursor.
- A batch of video IDs failing to fully resolve (e.g. a deleted/private
  video) never aborts the run: missing IDs are logged and skipped.
- Quota/auth failures (quotaExceeded, keyInvalid, forbidden) abort the
  run immediately, matching the Meta clients' TokenExpiredError pattern.
"""
import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)

BASE_URL = "https://www.googleapis.com/youtube/v3"
PAGE_SIZE = 50
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2

# Reasons that mean "stop now, retrying won't help" -- same role as
# Meta's code-190 TokenExpiredError.
FATAL_ERROR_REASONS = {"quotaExceeded", "dailyLimitExceeded", "keyInvalid", "forbidden"}


class YouTubeAPIError(Exception):
    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


class FatalAPIError(YouTubeAPIError):
    """Fatal: quota exhausted, key invalid, or access forbidden."""


class YouTubeClient:
    def __init__(self, api_key=None, channel_id=None):
        self.api_key = api_key or config.YOUTUBE_API_KEY
        self.channel_id = channel_id or config.YOUTUBE_CHANNEL_ID
        self.session = requests.Session()
        self._uploads_playlist_id = None

    def _get(self, endpoint: str, params: dict) -> dict:
        params = {**params, "key": self.api_key}
        url = f"{BASE_URL}/{endpoint}"
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.get(url, params=params, timeout=30)
            if resp.ok:
                return resp.json()

            payload = _safe_json(resp)
            error = payload.get("error", {})
            reason = _first_error_reason(error)

            if reason in FATAL_ERROR_REASONS:
                raise FatalAPIError(
                    error.get("message", f"HTTP {resp.status_code}"), reason=reason
                )

            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt >= MAX_RETRIES:
                raise YouTubeAPIError(
                    error.get("message", f"HTTP {resp.status_code}"), reason=reason
                )

            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "Transient YouTube API error (http=%s, reason=%s) -- retrying in %ds "
                "(attempt %d/%d)",
                resp.status_code,
                reason,
                backoff,
                attempt,
                MAX_RETRIES,
            )
            time.sleep(backoff)
        raise YouTubeAPIError(f"Exhausted retries for GET {endpoint}")

    def get_channel_info(self) -> dict:
        payload = self._get(
            "channels", {"part": "snippet,contentDetails", "id": self.channel_id}
        )
        items = payload.get("items", [])
        if not items:
            raise YouTubeAPIError(f"Channel not found or not accessible: {self.channel_id}")
        channel = items[0]
        self._uploads_playlist_id = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        return {
            "id": channel["id"],
            "title": channel["snippet"]["title"],
            "uploads_playlist_id": self._uploads_playlist_id,
        }

    def get_all_video_ids(self):
        """Yields {"id": ..., "published_at": ...} for every video in the
        channel's uploads playlist, following pagination cursors."""
        if not self._uploads_playlist_id:
            self.get_channel_info()

        page_token = None
        seen = 0
        while True:
            params = {
                "part": "contentDetails",
                "playlistId": self._uploads_playlist_id,
                "maxResults": PAGE_SIZE,
            }
            if page_token:
                params["pageToken"] = page_token

            payload = self._get("playlistItems", params)
            for item in payload.get("items", []):
                seen += 1
                yield {
                    "id": item["contentDetails"]["videoId"],
                    "published_at": item["contentDetails"].get("videoPublishedAt"),
                }

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        log.info("Listed %d videos for channel %s", seen, self.channel_id)

    def get_video_details(self, video_ids: list) -> dict:
        """Returns {video_id: {snippet, statistics, contentDetails}}.
        Video IDs the API doesn't return (deleted/private since listing)
        are logged and simply absent from the result, same contract as
        the Meta clients' get_video_details/get_media_details."""
        results = {}
        for chunk in _chunks(video_ids, PAGE_SIZE):
            payload = self._get(
                "videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)}
            )
            items = payload.get("items", [])
            for item in items:
                results[item["id"]] = item

            found_ids = {item["id"] for item in items}
            for video_id in chunk:
                if video_id not in found_ids:
                    log.warning(
                        "Video %s not returned by the API (deleted/private?)", video_id
                    )
        return results


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def _first_error_reason(error: dict):
    errors = error.get("errors") or []
    return errors[0].get("reason") if errors else None

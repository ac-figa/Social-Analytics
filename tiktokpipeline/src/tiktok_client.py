"""
Thin, resilient client for the TikTok API v2 (Display API via Login Kit
OAuth).

Unlike the Meta/YouTube clients, there's no permanent token: every run
must call authenticate() first to exchange the stored refresh token for a
fresh access token (expires in 24h). TikTok may rotate the refresh token
itself on every exchange -- authenticate() persists a rotated one back to
.env via config.update_refresh_token() so this never needs manual
attention after the initial OAuth login (see docs/SETUP.md).

video/list/ already returns full stats (views, likes, comments, shares)
per video in the same response that lists them -- no separate details/
insights call needed, matching YouTube's simplicity rather than the Meta
pipelines' batch+fallback dance.
"""
import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)

BASE_URL = "https://open.tiktokapis.com/v2"
PAGE_SIZE = 20  # video/list/'s max per page
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2

VIDEO_FIELDS = "id,title,create_time,duration,view_count,like_count,comment_count,share_count"


class TikTokAPIError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class TokenExpiredError(TikTokAPIError):
    """Fatal: the refresh token itself is invalid/expired/revoked --
    requires a fresh OAuth login (see docs/SETUP.md), not just a retry."""


class TikTokClient:
    def __init__(self, client_key=None, client_secret=None, refresh_token=None):
        self.client_key = client_key or config.TIKTOK_CLIENT_KEY
        self.client_secret = client_secret or config.TIKTOK_CLIENT_SECRET
        self.refresh_token = refresh_token or config.TIKTOK_REFRESH_TOKEN
        self.session = requests.Session()
        self.access_token = None
        self.open_id = None

    def authenticate(self) -> None:
        """Exchanges the refresh token for a fresh access token. Must be
        called once before any other method."""
        resp = self.session.post(
            f"{BASE_URL}/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        payload = _safe_json(resp)
        if "access_token" not in payload:
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else str(payload)
            raise TokenExpiredError(message or "Token refresh failed with no error message")

        self.access_token = payload["access_token"]
        self.open_id = payload.get("open_id")

        new_refresh_token = payload.get("refresh_token")
        if new_refresh_token:
            config.update_refresh_token(new_refresh_token)
            self.refresh_token = new_refresh_token

    def _post(self, endpoint: str, params: dict = None, json_body: dict = None) -> dict:
        if not self.access_token:
            raise TikTokAPIError("Call authenticate() before making API requests.")

        url = f"{BASE_URL}/{endpoint}"
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self.session.post(
                url,
                params=params or {},
                json=json_body or {},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            payload = _safe_json(resp)
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None

            if resp.ok and code in (None, "ok"):
                return payload

            if code == "access_token_invalid":
                raise TokenExpiredError(
                    error.get("message", "Access token invalid") if isinstance(error, dict) else "Access token invalid",
                    code=code,
                )

            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt >= MAX_RETRIES:
                message = (
                    error.get("message", f"HTTP {resp.status_code}")
                    if isinstance(error, dict)
                    else f"HTTP {resp.status_code}"
                )
                raise TikTokAPIError(message, code=code)

            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "Transient TikTok API error (http=%s, code=%s) -- retrying in %ds "
                "(attempt %d/%d)",
                resp.status_code,
                code,
                backoff,
                attempt,
                MAX_RETRIES,
            )
            time.sleep(backoff)
        raise TikTokAPIError(f"Exhausted retries for POST {endpoint}")

    def get_all_videos(self):
        """Yields raw video dicts (id, title, create_time, duration,
        view_count, like_count, comment_count, share_count) for every
        video on the authenticated account, following cursor pagination."""
        cursor = None
        seen = 0
        while True:
            body = {"max_count": PAGE_SIZE}
            if cursor:
                body["cursor"] = cursor

            payload = self._post("video/list/", params={"fields": VIDEO_FIELDS}, json_body=body)
            data = payload.get("data", {})
            videos = data.get("videos", [])
            for video in videos:
                seen += 1
                yield video

            if not data.get("has_more"):
                break
            cursor = data.get("cursor")

        log.info("Listed %d videos for open_id %s", seen, self.open_id)


def _safe_json(resp) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}

"""Environment-driven configuration. Loaded once at import time.

Unlike the Meta/YouTube pipelines, TikTok's access token expires every 24
hours and must be refreshed using TIKTOK_REFRESH_TOKEN -- and TikTok may
rotate the refresh token itself on every refresh. update_refresh_token()
below writes the new value back into .env so this is fully automatic
across runs; there's nothing to update by hand after the one-time OAuth
login that produced the first refresh token (see docs/SETUP.md).
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_PATH)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"See .env.example / docs/SETUP.md."
        )
    return value


TIKTOK_CLIENT_KEY = _require("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = _require("TIKTOK_CLIENT_SECRET")
TIKTOK_REFRESH_TOKEN = _require("TIKTOK_REFRESH_TOKEN")
TIKTOK_USERNAME = _require("TIKTOK_USERNAME")  # for building permalinks -- not returned by
                                                 # user.info.basic scope, see docs/SETUP.md

BQ_PROJECT_ID = _require("BQ_PROJECT_ID")
BQ_DATASET = os.environ.get("BQ_DATASET", "tiktok_analytics")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def update_refresh_token(new_refresh_token: str) -> None:
    """Persists a rotated refresh token back into .env so the next run
    picks it up automatically -- TikTok may issue a new one on every
    refresh, unlike a static API key or a permanent System User token."""
    global TIKTOK_REFRESH_TOKEN
    if new_refresh_token == TIKTOK_REFRESH_TOKEN:
        return
    set_key(str(_ENV_PATH), "TIKTOK_REFRESH_TOKEN", new_refresh_token)
    TIKTOK_REFRESH_TOKEN = new_refresh_token
    logging.getLogger(__name__).info("Rotated TIKTOK_REFRESH_TOKEN persisted to .env")

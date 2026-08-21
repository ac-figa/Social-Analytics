"""Environment-driven configuration. Loaded once at import time.

Reuses the same Meta App / System User token as the Instagram pipeline
(META_ACCESS_TOKEN) -- Facebook Page video access is part of the same
Meta Business permission grant, see docs/SETUP.md. FB_PAGE_ID is the one
new value: the Facebook Page ID, which docs/SETUP.md Step 3 of the
Instagram pipeline's own setup already has you look up along the way.
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"See .env.example / docs/SETUP.md."
        )
    return value


META_ACCESS_TOKEN = _require("META_ACCESS_TOKEN")
FB_PAGE_ID = _require("FB_PAGE_ID")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

BQ_PROJECT_ID = _require("BQ_PROJECT_ID")
BQ_DATASET = os.environ.get("BQ_DATASET", "facebook_analytics")

# See instagramanalyticspipeline/src/config.py's twin of this value --
# same reasoning, kept consistent across platforms.
INSIGHTS_REFRESH_DAYS = int(os.environ.get("INSIGHTS_REFRESH_DAYS", "45"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

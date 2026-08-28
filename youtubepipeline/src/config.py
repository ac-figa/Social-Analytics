"""Environment-driven configuration. Loaded once at import time."""
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


YOUTUBE_API_KEY = _require("YOUTUBE_API_KEY")
YOUTUBE_CHANNEL_ID = _require("YOUTUBE_CHANNEL_ID")

BQ_PROJECT_ID = _require("BQ_PROJECT_ID")
BQ_DATASET = os.environ.get("BQ_DATASET", "youtube_analytics")

# See instagramanalyticspipeline/src/config.py's twin of this value.
INSIGHTS_REFRESH_DAYS = int(os.environ.get("INSIGHTS_REFRESH_DAYS", "45"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

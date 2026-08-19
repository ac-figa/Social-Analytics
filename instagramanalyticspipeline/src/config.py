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


META_ACCESS_TOKEN = _require("META_ACCESS_TOKEN")
IG_USER_ID = _require("IG_USER_ID")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

BQ_PROJECT_ID = _require("BQ_PROJECT_ID")
BQ_DATASET = os.environ.get("BQ_DATASET", "instagram_analytics")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

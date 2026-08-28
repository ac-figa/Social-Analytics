"""Environment-driven configuration. Loaded once at import time.

Supports more than one Instagram account sharing this same pipeline/table
(e.g. a second brand page) via the ENV_FILE override: create a second env
file (e.g. .env.calciobros, copied from .env.example with that account's
own META_ACCESS_TOKEN/IG_USER_ID) and run

  ENV_FILE=.env.calciobros python -m src.pipeline

instead of the normal `python -m src.pipeline`. Both accounts' posts land
in the same instagram_master table (and the same shared content_items
layer), distinguished by the Account_ID/Account_Username columns that are
already populated per-row -- see bigquery_store.mark_missing_as_deleted()
for why that per-account scoping matters.
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV_FILE", ".env"))


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

# Posts published more than this many days ago skip the detail/insights
# refresh each run (their existing Instagram_Master row is left as-is) --
# older content's numbers barely move, and re-fetching them every run
# burns API calls and runtime for no real benefit. Still listed every run
# so nothing gets wrongly marked deleted; just not re-synced.
INSIGHTS_REFRESH_DAYS = int(os.environ.get("INSIGHTS_REFRESH_DAYS", "45"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

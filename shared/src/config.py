"""Environment-driven configuration for the shared cross-platform layer.

Reuses the same GCP project as the platform pipelines, but writes to its
own dataset -- platform pipelines keep their existing tables untouched;
this is an additive layer on top.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Explicit path rather than a bare load_dotenv() -- this module is meant to
# be run as `python -m shared.src.run_matching` from the repo root (see
# that module's docstring), where a CWD-relative search would never find
# shared/.env. It happens to also get sourced indirectly when a platform
# pipeline's own run picks up its own .env via CWD (see each pipeline's
# _sync_to_shared_content_layer) -- that path keeps working unchanged,
# since load_dotenv() never overwrites an already-set env var.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. See .env.example."
        )
    return value


BQ_PROJECT_ID = _require("BQ_PROJECT_ID")
SHARED_BQ_DATASET = os.environ.get("SHARED_BQ_DATASET", "social_analytics")

# Used by backfill_instagram_duration.py and cleanup_deleted_content.py,
# which reach directly into each platform's own dataset (all in the same
# GCP project) -- these default to each pipeline's own BQ_DATASET default,
# so no extra .env setup is needed unless you customized BQ_DATASET in
# that pipeline's own .env, in which case set the matching override here.
IG_BQ_DATASET = os.environ.get("IG_BQ_DATASET", "instagram_analytics")
FB_BQ_DATASET = os.environ.get("FB_BQ_DATASET", "facebook_analytics")
YT_BQ_DATASET = os.environ.get("YT_BQ_DATASET", "youtube_analytics")
TT_BQ_DATASET = os.environ.get("TT_BQ_DATASET", "tiktok_analytics")

# Single source of truth for each platform's dataset/master table/ID
# column -- webapp/src/config.py's PLATFORM_CONFIG mirrors the
# classifications-table half of this; kept separate since the dashboard
# needs its own env vars loaded from webapp/.env, not shared/.env.
PLATFORM_CONFIG = {
    "Instagram": {
        "dataset": IG_BQ_DATASET, "master_table": "instagram_master",
        "classifications_table": "instagram_classifications", "id_column": "Post_ID",
    },
    "Facebook": {
        "dataset": FB_BQ_DATASET, "master_table": "facebook_master",
        "classifications_table": "facebook_classifications", "id_column": "Video_ID",
    },
    "YouTube": {
        "dataset": YT_BQ_DATASET, "master_table": "youtube_master",
        "classifications_table": "youtube_classifications", "id_column": "Video_ID",
    },
    "TikTok": {
        "dataset": TT_BQ_DATASET, "master_table": "tiktok_master",
        "classifications_table": "tiktok_classifications", "id_column": "Video_ID",
    },
}

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

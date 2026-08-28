"""
Environment-driven configuration for the local classification dashboard.

Deliberately only needs BQ_PROJECT_ID -- unlike each platform pipeline's
own config.py, this never needs an API token, since the dashboard only
reads/writes BigQuery. This is also why it's safe to run from any
directory without the CWD-relative .env problems the pipelines have (see
shared/src/config.py's docstring for that story) -- load_dotenv() here
uses an explicit path to webapp/.env for the same reason.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}. See .env.example.")
    return value


BQ_PROJECT_ID = _require("BQ_PROJECT_ID")
SHARED_BQ_DATASET = os.environ.get("SHARED_BQ_DATASET", "social_analytics")

# Google sign-in (see src/auth.py). Only required when actually deployed
# somewhere reachable off your own machine -- running locally with
# `python3 app.py` never needs these at all (see app.py: auth is skipped
# entirely if GOOGLE_CLIENT_ID is unset).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
ALLOWED_EMAILS = {
    e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
}
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key-change-me")

# Each platform's own dataset + classifications table + master table's ID
# column -- used to propagate a group's Partnership/Content_Type down into
# every member platform's own *_classifications table when the dashboard
# classifies a group, so instagram_master/facebook_master/etc. (each
# pipeline's own reporting surface) reflect it too, not just content_groups.
PLATFORM_CONFIG = {
    "Instagram": {
        "dataset": os.environ.get("IG_BQ_DATASET", "instagram_analytics"),
        "classifications_table": "instagram_classifications",
        "id_column": "Post_ID",
    },
    "Facebook": {
        "dataset": os.environ.get("FB_BQ_DATASET", "facebook_analytics"),
        "classifications_table": "facebook_classifications",
        "id_column": "Video_ID",
    },
    "YouTube": {
        "dataset": os.environ.get("YT_BQ_DATASET", "youtube_analytics"),
        "classifications_table": "youtube_classifications",
        "id_column": "Video_ID",
    },
    "TikTok": {
        "dataset": os.environ.get("TT_BQ_DATASET", "tiktok_analytics"),
        "classifications_table": "tiktok_classifications",
        "id_column": "Video_ID",
    },
}

# Each pipeline directory name, relative to the repo root -- used by
# src/sync.py to shell out to `python -m src.pipeline` in each one.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_DIRS = {
    "Instagram": _REPO_ROOT / "instagramanalyticspipeline",
    "Facebook": _REPO_ROOT / "facebookpipeline",
    "YouTube": _REPO_ROOT / "youtubepipeline",
    "TikTok": _REPO_ROOT / "tiktokpipeline",
}
SHARED_DIR = _REPO_ROOT / "shared"

# Second-account syncs (a brand page sharing a platform's tables via its
# own env file -- see instagramanalyticspipeline/src/config.py and
# tiktokpipeline/src/config.py's ENV_FILE override). src/sync.py runs
# each of these after the main four, setting ENV_FILE for that one
# subprocess only. env_file is relative to pipeline_dir; a missing file
# is skipped (logged, not fatal) rather than failing the whole sync --
# lets this list grow ahead of every account actually being set up yet.
EXTRA_ACCOUNT_SYNCS = [
    {"label": "Instagram (Calcio Bros)", "pipeline_dir": PIPELINE_DIRS["Instagram"], "env_file": ".env.calciobros"},
    {"label": "TikTok (Calcio Bros)", "pipeline_dir": PIPELINE_DIRS["TikTok"], "env_file": ".env.calciobros"},
]

# The Cloud Run deployment (see deploy/README.md) only ever copies
# webapp/ and shared/ into the image -- never the four pipeline
# directories or their .env files, since Sync would otherwise need every
# platform's live API credentials baked into a container reachable from
# the internet. Detecting their absence is how the app tells "running
# locally" apart from "deployed" without a separate env var to keep in
# sync -- Sync just disables itself with an explanation instead of
# failing confusingly.
SYNC_AVAILABLE = all(d.is_dir() for d in PIPELINE_DIRS.values())

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

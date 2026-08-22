"""
Main entrypoint: pulls TikTok video data + stats, reattaches manual
classifications, upserts TikTok_Master, marks vanished videos, appends
today's TikTok_Insights_History snapshot, and mirrors into the shared
cross-platform content layer for cross-platform matching.

No --full flag or INSIGHTS_REFRESH_DAYS window here, unlike the other
pipelines: video/list/ already returns full stats for every video in the
same call that lists them (see tiktok_client.py), so there's no separate
expensive detail/insights fetch to skip for old content -- every run
already re-syncs everything at the same (low) cost.

Run:  python -m src.pipeline
"""
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import bigquery_store, suggestions, transform
from .tiktok_client import TikTokAPIError, TikTokClient, TokenExpiredError

# See instagramanalyticspipeline/src/pipeline.py for why this path math:
# src/ -> tiktokpipeline/ -> repo root, which also contains shared/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger(__name__)


def run() -> int:
    client = TikTokClient()

    try:
        client.authenticate()
    except TokenExpiredError as e:
        log.error(
            "Fatal: refresh token invalid/expired: %s. A fresh OAuth login is needed -- "
            "see docs/SETUP.md.",
            e,
        )
        return 1

    log.info("Fetching video list ...")
    try:
        videos = list(client.get_all_videos())
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1
    except TikTokAPIError as e:
        log.error("Fatal: could not list videos: %s", e)
        return 1

    if not videos:
        log.warning("No videos returned -- nothing to do.")
        return 0

    log.info("Found %d videos.", len(videos))

    bq_client = bigquery_store.get_client()
    bigquery_store.ensure_schema(bq_client)
    classifications = bigquery_store.load_classifications(bq_client)
    brand_map = suggestions.load_brand_keywords()

    rows = []
    failed_video_ids = []

    for video in videos:
        video_id = video.get("id")
        try:
            row = transform.build_master_row(video)

            existing = classifications.get(video_id, {})
            row["Partnership"] = existing.get("Partnership") or "Unclassified"
            row["Content_Type"] = existing.get("Content_Type") or "Unclassified"
            row["Suggested_Partnership"] = suggestions.suggest_partnership(
                row.get("Title"), brand_map
            )

            rows.append(row)
        except Exception as e:  # noqa: BLE001 -- one bad video must not kill the run
            log.error("Failed to process Video_ID=%s: %s", video_id, e)
            failed_video_ids.append(video_id)

    all_video_ids = [v["id"] for v in videos]
    bigquery_store.upsert_master_rows(bq_client, rows)
    bigquery_store.mark_missing_as_deleted(bq_client, all_video_ids)
    _sync_to_shared_content_layer(rows)

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    history_rows = [transform.build_history_row(r, snapshot_date) for r in rows]
    bigquery_store.insert_history_snapshot(bq_client, history_rows, snapshot_date)

    log.info(
        "Run complete: %d upserted, %d failed. Snapshot date: %s.",
        len(rows),
        len(failed_video_ids),
        snapshot_date,
    )
    if failed_video_ids:
        log.warning("Failed Video_IDs: %s", ", ".join(failed_video_ids))

    return 0


def _sync_to_shared_content_layer(rows: list) -> None:
    """See instagramanalyticspipeline/src/pipeline.py's twin of this
    function for the full rationale -- best-effort, never fails this
    pipeline's own successful TikTok ingestion."""
    try:
        from shared.src import content_store, matching
    except ImportError:
        log.warning("Shared content layer not importable -- skipping cross-platform sync.")
        return

    try:
        content_items = [transform.to_content_item(r) for r in rows]
        shared_client = content_store.get_client()
        content_store.ensure_schema(shared_client)
        content_store.upsert_content_items(shared_client, content_items)

        ungrouped = content_store.get_ungrouped_items(shared_client)
        if ungrouped:
            candidates = matching.find_candidate_groups(ungrouped)
            for candidate in candidates:
                content_store.create_group(
                    shared_client,
                    content_ids=candidate["content_ids"],
                    match_method="auto",
                    match_confidence=candidate["confidence"],
                    confirmed=candidate["auto_confirm"],
                    updated_by="tiktok_pipeline",
                )
            log.info("Cross-platform sync: %d candidate group(s) from this run.", len(candidates))
    except Exception as e:  # noqa: BLE001 -- shared-layer issues must not fail this pipeline
        log.warning("Cross-platform content sync failed (non-fatal): %s", e)


if __name__ == "__main__":
    sys.exit(run())

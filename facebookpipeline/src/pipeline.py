"""
Main entrypoint: pulls Facebook Page videos + insights, reattaches manual
classifications, upserts Facebook_Master, marks vanished videos, appends
today's Facebook_Insights_History snapshot, and mirrors into the shared
cross-platform content layer for cross-platform matching.

Run:               python -m src.pipeline
Full backfill:      python -m src.pipeline --full
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bigquery_store, config, suggestions, transform
from .graph_client import FacebookGraphClient, GraphAPIError, TokenExpiredError

# See instagramanalyticspipeline/src/pipeline.py for why this path math:
# src/ -> facebookpipeline/ -> repo root, which also contains shared/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger(__name__)


def run(full_refresh: bool = False) -> int:
    client = FacebookGraphClient()

    try:
        page_info = client.get_page_info()
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1

    log.info("Fetching video list for Page %s ...", page_info.get("name"))
    try:
        video_list = list(client.get_all_video_ids())
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1
    except GraphAPIError as e:
        log.error("Fatal: could not list videos: %s", e)
        return 1

    if not video_list:
        log.warning("No videos returned for this Page -- nothing to do.")
        return 0

    all_video_ids = [v["id"] for v in video_list]
    log.info("Found %d videos.", len(all_video_ids))

    # See instagramanalyticspipeline/src/pipeline.py's twin of this block --
    # only re-fetch details/insights for recently published videos. --full
    # (or full_refresh=True) bypasses this for a one-off backfill.
    if full_refresh:
        log.info("Full refresh requested -- re-fetching details/insights for all %d video(s).", len(video_list))
        refresh_items = video_list
        skipped_count = 0
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.INSIGHTS_REFRESH_DAYS)
        refresh_items = [
            v
            for v in video_list
            if (published := transform.parse_timestamp(v.get("created_time"))) is None
            or published >= cutoff
        ]
        skipped_count = len(video_list) - len(refresh_items)
        if skipped_count:
            log.info(
                "Skipping detail/insights refresh for %d video(s) published more "
                "than %d days ago. Run with --full to refresh everything.",
                skipped_count,
                config.INSIGHTS_REFRESH_DAYS,
            )

    refresh_ids = [v["id"] for v in refresh_items]
    details = client.get_video_details(refresh_ids)
    insights = client.get_post_insights(details)

    bq_client = bigquery_store.get_client()
    bigquery_store.ensure_schema(bq_client)
    classifications = bigquery_store.load_classifications(bq_client)
    brand_map = suggestions.load_brand_keywords()

    rows = []
    failed_video_ids = []
    orphan_video_ids = []

    for video_id in refresh_ids:
        try:
            detail = details.get(video_id)
            if detail is None:
                log.warning("Skipping Video_ID=%s: no video detail available.", video_id)
                failed_video_ids.append(video_id)
                continue

            if detail.get("likes") is None:
                # Confirmed live (Aug 2026): a real, published Page post
                # always returns a `likes` object (even {total_count: 0}
                # for zero likes) -- these video assets never do, have no
                # `description`, and don't correspond to any entry in the
                # Page's own /posts feed at all. Not Reels either (checked
                # /video_reels directly). Best guess: leftover/duplicate
                # upload artifacts, not real published content -- treated
                # as noise rather than pulled into Facebook_Master.
                log.info("Skipping Video_ID=%s: not a real Page post (no likes object).", video_id)
                orphan_video_ids.append(video_id)
                continue

            row = transform.build_master_row(
                video_detail=detail,
                insights=insights.get(video_id),
                page_info=page_info,
            )

            existing = classifications.get(video_id, {})
            row["Partnership"] = existing.get("Partnership") or "Unclassified"
            row["Content_Type"] = existing.get("Content_Type") or "Unclassified"
            row["Suggested_Partnership"] = suggestions.suggest_partnership(
                detail.get("description"), brand_map
            )

            rows.append(row)
        except TokenExpiredError:
            raise
        except Exception as e:  # noqa: BLE001 -- one bad video must not kill the run
            log.error("Failed to process Video_ID=%s: %s", video_id, e)
            failed_video_ids.append(video_id)

    bigquery_store.upsert_master_rows(bq_client, rows)
    # Uses every video still listed by the API (all_video_ids), not just the
    # ones refreshed this run -- see the Instagram pipeline's twin comment.
    # Orphan video assets (see the `likes is None` check above) are removed
    # from this list so mark_missing_as_deleted correctly soft-deletes any
    # that were previously synced before this filter existed, instead of
    # leaving them stuck as Active forever.
    active_ids = [vid for vid in all_video_ids if vid not in orphan_video_ids]
    bigquery_store.mark_missing_as_deleted(bq_client, active_ids)
    _sync_to_shared_content_layer(rows)

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    history_rows = [transform.build_history_row(r, snapshot_date) for r in rows]
    bigquery_store.insert_history_snapshot(bq_client, history_rows, snapshot_date)

    log.info(
        "Run complete: %d upserted, %d failed, %d orphan video asset(s) skipped, "
        "%d skipped (outside refresh window). Snapshot date: %s.",
        len(rows),
        len(failed_video_ids),
        len(orphan_video_ids),
        skipped_count,
        snapshot_date,
    )
    if failed_video_ids:
        log.warning("Failed Video_IDs: %s", ", ".join(failed_video_ids))

    return 0


def _sync_to_shared_content_layer(rows: list) -> None:
    """See instagramanalyticspipeline/src/pipeline.py's twin of this
    function for the full rationale -- best-effort, never fails this
    pipeline's own successful Facebook ingestion."""
    try:
        from shared.src import content_store, run_matching
    except ImportError:
        log.warning("Shared content layer not importable -- skipping cross-platform sync.")
        return

    try:
        content_items = [transform.to_content_item(r) for r in rows]
        shared_client = content_store.get_client()
        content_store.ensure_schema(shared_client)
        content_store.upsert_content_items(shared_client, content_items)

        ungrouped = content_store.get_ungrouped_items(shared_client)
        stats = run_matching.match_items(shared_client, ungrouped, updated_by="facebook_pipeline")
        log.info(
            "Cross-platform sync: %d new group(s), %d item(s) added to existing groups.",
            stats["created"] + stats["pending"],
            stats["added_existing"] + stats["pending_existing"],
        )
    except Exception as e:  # noqa: BLE001 -- shared-layer issues must not fail this pipeline
        log.warning("Cross-platform content sync failed (non-fatal): %s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Refresh every video's details/insights, ignoring INSIGHTS_REFRESH_DAYS "
        "(a full backfill instead of the normal incremental run).",
    )
    args = parser.parse_args()
    sys.exit(run(full_refresh=args.full))

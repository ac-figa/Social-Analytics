"""
Main entrypoint: pulls Instagram media + insights, reattaches manual
classifications, upserts Instagram_Master, marks vanished posts, and
appends today's Instagram_Insights_History snapshot.

Run:               python -m src.pipeline
Full backfill:      python -m src.pipeline --full
"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bigquery_store, config, suggestions, transform
from .graph_client import GraphAPIError, InstagramGraphClient, RateLimitedError, TokenExpiredError

# Repo root (two levels up from this file: src/ -> instagramanalyticspipeline/
# -> repo root) so the shared cross-platform content layer is importable
# both in local dev (`cd instagramanalyticspipeline && python -m src.pipeline`)
# and from the Docker image, which mirrors this same layout under /app.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger(__name__)


def run(full_refresh: bool = False) -> int:
    client = InstagramGraphClient()

    try:
        account_info = client.get_account_info()
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1

    log.info("Fetching media list for @%s ...", account_info.get("username"))
    try:
        media_list = list(client.get_all_media_ids())
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1
    except GraphAPIError as e:
        log.error("Fatal: could not list media: %s", e)
        return 1

    if not media_list:
        log.warning("No media returned for this account -- nothing to do.")
        return 0

    all_media_ids = [m["id"] for m in media_list]
    reel_count = sum(1 for m in media_list if m.get("media_product_type") == "REELS")
    log.info("Found %d media items (%d Reels).", len(all_media_ids), reel_count)

    # Only re-fetch details/insights for recently published posts -- older
    # content's numbers barely move, and re-syncing it every run wastes API
    # calls and runtime. Everything is still listed above (cheap) so
    # mark_missing_as_deleted below never wrongly flags a dormant-but-still-
    # live post; it just won't get its Instagram_Master row refreshed.
    # --full (or full_refresh=True) bypasses this entirely for a one-off
    # backfill of every post's numbers.
    if full_refresh:
        log.info("Full refresh requested -- re-fetching details/insights for all %d post(s).", len(media_list))
        refresh_items = media_list
        skipped_count = 0
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.INSIGHTS_REFRESH_DAYS)
        refresh_items = [
            m
            for m in media_list
            if (published := transform.parse_timestamp(m.get("timestamp"))) is None
            or published >= cutoff
        ]
        skipped_count = len(media_list) - len(refresh_items)
        if skipped_count:
            log.info(
                "Skipping detail/insights refresh for %d post(s) published more "
                "than %d days ago. Run with --full to refresh everything.",
                skipped_count,
                config.INSIGHTS_REFRESH_DAYS,
            )

    refresh_ids = [m["id"] for m in refresh_items]
    try:
        details = client.get_media_details(refresh_ids)
        insights = client.get_media_insights(refresh_items)
    except RateLimitedError as e:
        log.error(
            "Fatal: %s Nothing was upserted this run -- re-run the same command "
            "(with --full if that's what you used) once the rate limit clears.",
            e,
        )
        return 1
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1

    bq_client = bigquery_store.get_client()
    bigquery_store.ensure_schema(bq_client)
    classifications = bigquery_store.load_classifications(bq_client)
    brand_map = suggestions.load_brand_keywords()

    rows = []
    failed_post_ids = []

    try:
        for item in refresh_items:
            post_id = item["id"]
            try:
                detail = details.get(post_id)
                if detail is None:
                    log.warning("Skipping Post_ID=%s: no media detail available.", post_id)
                    failed_post_ids.append(post_id)
                    continue

                collaborators = []
                if item.get("media_product_type") == "REELS":
                    collaborators = client.get_collaborators(post_id)

                row = transform.build_master_row(
                    media_detail=detail,
                    insights=insights.get(post_id),
                    collaborators=collaborators,
                    account_info=account_info,
                )

                existing = classifications.get(post_id, {})
                row["Partnership"] = existing.get("Partnership") or "Unclassified"
                row["Content_Type"] = existing.get("Content_Type") or "Unclassified"
                row["Suggested_Partnership"] = suggestions.suggest_partnership(
                    detail.get("caption"), collaborators, brand_map
                )
                # User-owned free-text fields: only meaningful default on first
                # INSERT -- the MERGE never overwrites them on existing rows.
                row["Data_Comment"] = None
                row["Data"] = None

                rows.append(row)
            except (TokenExpiredError, RateLimitedError):
                raise
            except Exception as e:  # noqa: BLE001 -- one bad post must not kill the run
                log.error("Failed to process Post_ID=%s: %s", post_id, e)
                failed_post_ids.append(post_id)
    except RateLimitedError as e:
        log.error(
            "Fatal: %s %d post(s) already upserted before the limit hit; the rest "
            "weren't touched -- re-run the same command once the rate limit clears.",
            e,
            len(rows),
        )
        return 1
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1

    bigquery_store.upsert_master_rows(bq_client, rows)
    # Uses every post still listed by the API (all_media_ids), not just the
    # ones refreshed this run -- otherwise every skipped, still-live post
    # would get wrongly marked Deleted_or_Unavailable. Scoped to this
    # account's Account_ID -- see mark_missing_as_deleted()'s docstring.
    bigquery_store.mark_missing_as_deleted(bq_client, all_media_ids, account_info["id"])
    _sync_to_shared_content_layer(rows)

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    history_rows = [transform.build_history_row(r, snapshot_date) for r in rows]
    bigquery_store.insert_history_snapshot(bq_client, history_rows, snapshot_date)

    log.info(
        "Run complete: %d upserted, %d failed, %d skipped (outside refresh window). Snapshot date: %s.",
        len(rows),
        len(failed_post_ids),
        skipped_count,
        snapshot_date,
    )
    if failed_post_ids:
        log.warning("Failed Post_IDs: %s", ", ".join(failed_post_ids))

    return 0


def _sync_to_shared_content_layer(rows: list) -> None:
    """Mirrors this run's rows into the shared cross-platform content_items
    table and runs auto-matching over whatever's newly ungrouped, so this
    account's posts can be linked to the same content on YouTube/TikTok/
    Facebook. Best-effort: a failure here (e.g. the shared dataset isn't
    provisioned yet) must never fail the Instagram-only ingestion that
    already succeeded above.
    """
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
        stats = run_matching.match_items(shared_client, ungrouped, updated_by="instagram_pipeline")
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
        help="Refresh every post's details/insights, ignoring INSIGHTS_REFRESH_DAYS "
        "(a full backfill instead of the normal incremental run).",
    )
    args = parser.parse_args()
    sys.exit(run(full_refresh=args.full))

"""
Main entrypoint: pulls Instagram media + insights, reattaches manual
classifications, upserts Instagram_Master, marks vanished posts, and
appends today's Instagram_Insights_History snapshot.

Run:  python -m src.pipeline
"""
import logging
import sys
from datetime import datetime, timezone

from . import bigquery_store, suggestions, transform
from .graph_client import GraphAPIError, InstagramGraphClient, TokenExpiredError

log = logging.getLogger(__name__)


def run() -> int:
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

    media_ids = [m["id"] for m in media_list]
    reel_count = sum(1 for m in media_list if m.get("media_product_type") == "REELS")
    log.info("Found %d media items (%d Reels).", len(media_ids), reel_count)

    details = client.get_media_details(media_ids)
    insights = client.get_media_insights(media_list)

    bq_client = bigquery_store.get_client()
    bigquery_store.ensure_schema(bq_client)
    classifications = bigquery_store.load_classifications(bq_client)
    brand_map = suggestions.load_brand_keywords()

    rows = []
    failed_post_ids = []

    for item in media_list:
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
        except TokenExpiredError:
            raise
        except Exception as e:  # noqa: BLE001 -- one bad post must not kill the run
            log.error("Failed to process Post_ID=%s: %s", post_id, e)
            failed_post_ids.append(post_id)

    bigquery_store.upsert_master_rows(bq_client, rows)
    bigquery_store.mark_missing_as_deleted(bq_client, [r["Post_ID"] for r in rows])

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    history_rows = [transform.build_history_row(r, snapshot_date) for r in rows]
    bigquery_store.insert_history_snapshot(bq_client, history_rows, snapshot_date)

    log.info(
        "Run complete: %d upserted, %d failed. Snapshot date: %s.",
        len(rows),
        len(failed_post_ids),
        snapshot_date,
    )
    if failed_post_ids:
        log.warning("Failed Post_IDs: %s", ", ".join(failed_post_ids))

    return 0


if __name__ == "__main__":
    sys.exit(run())

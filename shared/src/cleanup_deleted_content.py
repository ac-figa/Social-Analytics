"""
Removes content_items (and any content_group_members linking to them)
whose underlying platform post is no longer Active -- genuinely deleted
on the platform, or (Facebook specifically) a video asset that was never
a real published post to begin with (see facebookpipeline/src/graph_client.py
"not a real Page post" filter, added Aug 2026).

Why this is needed at all: each platform pipeline's own mark_missing_as_deleted
correctly flags a vanished post as Deleted_or_Unavailable in its own
*_master table, but nothing propagates that into content_items -- a
pipeline's _sync_to_shared_content_layer only ever *adds/updates* rows for
whatever it just fetched, it never removes a row for something that
stopped being fetched. Without this, a deleted post stays stuck looking
Active (and possibly still "Matched") in the dashboard forever.

Safe to run any time -- a no-op for platforms/posts with nothing stale.
Deletes rather than soft-marks, since content_items is documented as a
mirror of each platform's own master table (the real historical record),
not itself a permanent record -- see shared/README.md.

  python -m shared.src.cleanup_deleted_content

Note: BigQuery refuses DELETE on rows still in the streaming buffer
(recently inserted via a streaming insert, e.g. content_group_members
added by run_matching.py in roughly the last hour) -- if you see that
error, it's not a bug, just re-run this later.
"""
import logging
import sys

from google.cloud import bigquery

from . import config

log = logging.getLogger(__name__)


def run() -> int:
    client = bigquery.Client(project=config.BQ_PROJECT_ID)
    shared_ref = f"{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}"

    total_members_removed = 0
    total_items_removed = 0

    for platform, p in config.PLATFORM_CONFIG.items():
        master_ref = f"{config.BQ_PROJECT_ID}.{p['dataset']}.{p['master_table']}"
        id_column = p["id_column"]

        stale_subquery = f"""
        SELECT ci.Content_ID
        FROM `{shared_ref}.content_items` ci
        LEFT JOIN `{master_ref}` m ON ci.Platform_Post_ID = m.{id_column}
        WHERE ci.Platform = @platform AND (m.{id_column} IS NULL OR m.API_Status != 'Active')
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("platform", "STRING", platform)]
        )

        members_result = client.query(
            f"DELETE FROM `{shared_ref}.content_group_members` WHERE Content_ID IN ({stale_subquery})",
            job_config=job_config,
        ).result()
        members_removed = members_result.num_dml_affected_rows or 0

        items_result = client.query(
            f"DELETE FROM `{shared_ref}.content_items` WHERE Content_ID IN ({stale_subquery})",
            job_config=job_config,
        ).result()
        items_removed = items_result.num_dml_affected_rows or 0

        if members_removed or items_removed:
            log.info(
                "%s: removed %d content_items row(s), %d group membership(s).",
                platform,
                items_removed,
                members_removed,
            )
        total_members_removed += members_removed
        total_items_removed += items_removed

    log.info(
        "Done. Removed %d content_items row(s) and %d group membership(s) total for content "
        "no longer Active on its platform.",
        total_items_removed,
        total_members_removed,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

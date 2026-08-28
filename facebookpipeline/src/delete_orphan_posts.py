"""
One-time cleanup: deletes facebook_master rows for orphan video assets that
were synced before pipeline.py started filtering them out at sync time (see
pipeline.py's orphan_video_ids handling and its module docstring). An
orphan is identified the same way live syncs identify one -- no caption and
no likes object at all (Description IS NULL AND Likes IS NULL); a real
post, even one genuinely published with no caption, always gets a likes
object back from the API (summary.total_count), even when that count is 0.

Only touches facebook_master. Afterwards, run
`python -m shared.src.cleanup_deleted_content` to remove the matching rows
from the shared content_items/content_group_members tables -- it already
does exactly that for anything whose platform master row goes missing, so
there's no separate cleanup needed here for the shared layer.

  python -m facebookpipeline.src.delete_orphan_posts
"""
import logging
import sys

from . import config
from .bigquery_store import get_client, MASTER_TABLE

log = logging.getLogger(__name__)


def run() -> int:
    client = get_client()
    table_ref = f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET}.{MASTER_TABLE}"

    n = list(
        client.query(
            f"SELECT COUNT(*) AS n FROM `{table_ref}` WHERE Description IS NULL AND Likes IS NULL"
        ).result()
    )[0]["n"]
    if n == 0:
        log.info("No orphan video assets found in facebook_master -- nothing to delete.")
        return 0

    log.info("Deleting %d orphan video asset row(s) from facebook_master...", n)
    client.query(f"DELETE FROM `{table_ref}` WHERE Description IS NULL AND Likes IS NULL").result()
    log.info(
        "Done. Now run `python -m shared.src.cleanup_deleted_content` to remove these "
        "from content_items/content_group_members too."
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

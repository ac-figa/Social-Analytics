"""
Backfills Instagram Duration from the matched Facebook cross-post's
Length -- the one gap the Instagram Graph API can never fill on its own
(see instagramanalyticspipeline/docs/API_NOTES.md "Confirmed gaps"), but
one this project already has the data to close: when a Reel is also
shared to Facebook, the two posts are the same video file, so Facebook's
`Length` field (which the Graph API *does* expose) is Instagram's
Duration.

Two steps, in order:
1. Instagram_Master.Duration <- matched Facebook post's Length. Relies on
   content_groups/content_group_members already having linked the two --
   run shared/src/run_matching.py first. Only pulls from *confirmed* group
   memberships, never a pending/unconfirmed match, since this writes a
   real number into Instagram_Master rather than just suggesting a link.
2. content_items.Duration (the shared cross-platform table matching.py
   actually reads) <- step 1's result. Without this second step, the
   matcher never sees Instagram's newly-filled Duration at all --
   Instagram/YouTube or Instagram/TikTok pairs with no Facebook post in
   between would keep falling back to the weaker caption+date scheme even
   after step 1 ran, since to_content_item() always produces Duration=None
   for Instagram (the Graph API never returns it) every pipeline run.

Run after matching (or on its own; it's a no-op if nothing new to fill):

  python -m shared.src.backfill_instagram_duration

Re-run shared/src/run_matching.py afterward to let YouTube/TikTok items
that only had a weak caption-based candidate re-match against Instagram
directly using the now-available duration signal.
"""
import logging
import sys

from google.cloud import bigquery

from . import config, content_store

log = logging.getLogger(__name__)


def run() -> int:
    client = content_store.get_client()

    shared_ref = f"{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}"
    ig_master_ref = f"{config.BQ_PROJECT_ID}.{config.IG_BQ_DATASET}.instagram_master"
    fb_master_ref = f"{config.BQ_PROJECT_ID}.{config.FB_BQ_DATASET}.facebook_master"

    master_query = f"""
    UPDATE `{ig_master_ref}` ig
    SET Duration = matched.Length
    FROM (
      SELECT ci_ig.Platform_Post_ID AS ig_post_id, fbm.Length AS Length
      FROM `{shared_ref}.content_group_members` m_ig
      JOIN `{shared_ref}.content_items` ci_ig
        ON m_ig.Content_ID = ci_ig.Content_ID AND ci_ig.Platform = 'Instagram'
      JOIN `{shared_ref}.content_group_members` m_fb
        ON m_ig.Group_ID = m_fb.Group_ID AND m_fb.Content_ID != m_ig.Content_ID
      JOIN `{shared_ref}.content_items` ci_fb
        ON m_fb.Content_ID = ci_fb.Content_ID AND ci_fb.Platform = 'Facebook'
      JOIN `{fb_master_ref}` fbm
        ON ci_fb.Platform_Post_ID = fbm.Video_ID
      WHERE m_ig.Confirmed = TRUE AND m_fb.Confirmed = TRUE AND fbm.Length IS NOT NULL
    ) matched
    WHERE ig.Post_ID = matched.ig_post_id AND ig.Duration IS NULL
    """
    result = client.query(master_query, job_config=bigquery.QueryJobConfig()).result()
    updated_master = result.num_dml_affected_rows or 0
    log.info("Backfilled Duration for %d Instagram_Master row(s) from matched Facebook posts.", updated_master)

    content_items_query = f"""
    UPDATE `{shared_ref}.content_items` ci
    SET Duration = igm.Duration
    FROM `{ig_master_ref}` igm
    WHERE ci.Platform = 'Instagram'
      AND ci.Platform_Post_ID = igm.Post_ID
      AND igm.Duration IS NOT NULL
      AND ci.Duration IS NULL
    """
    result = client.query(content_items_query, job_config=bigquery.QueryJobConfig()).result()
    updated_content_items = result.num_dml_affected_rows or 0
    log.info(
        "Propagated Duration into %d content_items row(s) so the matcher can use it directly.",
        updated_content_items,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

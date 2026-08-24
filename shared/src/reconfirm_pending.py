"""
One-time reconciliation: when AUTO_CONFIRM_SCORE changes, existing
pending (Confirmed=False) matches don't automatically get re-evaluated --
run_matching.py only ever looks at items with zero group membership, and
a pending match already has one. This applies the *current*
AUTO_CONFIRM_SCORE to whatever's already sitting in the pending queue,
confirming anything that clears the new bar.

Safe to run any time, not just after a threshold change -- a no-op if
nothing pending currently clears AUTO_CONFIRM_SCORE.

  python -m shared.src.reconfirm_pending
"""
import logging
import sys

from google.cloud import bigquery

from . import config, matching

log = logging.getLogger(__name__)


def run() -> int:
    client = bigquery.Client(project=config.BQ_PROJECT_ID)
    shared_ref = f"{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}"

    query = f"""
    UPDATE `{shared_ref}.content_group_members`
    SET Confirmed = TRUE
    WHERE Confirmed = FALSE AND Match_Confidence >= @auto_confirm_score
    """
    result = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "auto_confirm_score", "FLOAT64", matching.AUTO_CONFIRM_SCORE
                )
            ]
        ),
    ).result()
    updated = result.num_dml_affected_rows or 0
    log.info(
        "Confirmed %d previously-pending match(es) now clearing AUTO_CONFIRM_SCORE=%.2f.",
        updated,
        matching.AUTO_CONFIRM_SCORE,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

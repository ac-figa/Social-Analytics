"""
Runs the auto-matching heuristic over every ungrouped content_item and
creates content_groups for whatever it finds -- high-confidence matches
are Confirmed=True immediately, everything else is written Confirmed=False
for a human to accept/reject in the dashboard.

Run after any platform pipeline finishes (or on its own; it's a no-op if
there's nothing ungrouped):

  python -m shared.src.run_matching
"""
import logging
import sys

from . import content_store, matching

log = logging.getLogger(__name__)


def run() -> int:
    client = content_store.get_client()
    content_store.ensure_schema(client)

    items = content_store.get_ungrouped_items(client)
    if not items:
        log.info("No ungrouped content_items -- nothing to match.")
        return 0

    log.info("Matching %d ungrouped content_items...", len(items))
    candidates = matching.find_candidate_groups(items)

    created, pending = 0, 0
    for candidate in candidates:
        content_store.create_group(
            client,
            content_ids=candidate["content_ids"],
            match_method="auto",
            match_confidence=candidate["confidence"],
            confirmed=candidate["auto_confirm"],
            updated_by="auto_match",
        )
        if candidate["auto_confirm"]:
            created += 1
        else:
            pending += 1

    log.info(
        "Created %d confirmed group(s) and %d pending-review group(s) from %d ungrouped items.",
        created,
        pending,
        len(items),
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

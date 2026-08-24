"""
Runs the auto-matching heuristic over every ungrouped content_item and
creates content_groups for whatever it finds -- high-confidence matches
are Confirmed=True immediately, everything else is written Confirmed=False
for a human to accept/reject in the dashboard.

Two passes, in order:
1. Check each ungrouped item against every *existing* confirmed group's
   members first. This matters because get_ungrouped_items() only ever
   returns items with zero group membership -- without this pass, an item
   from a platform that started syncing later than the others (e.g.
   TikTok's real Facebook/Instagram counterpart was already claimed by an
   earlier run's group) would never be reconsidered, even though its real
   match already exists. Confirmed at 676 real TikTok items scoring 0
   matches in Aug 2026 before this pass existed.
2. Whatever's left after that clusters into brand-new groups among itself
   (the original behavior), same as before.

Run after any platform pipeline finishes (or on its own; it's a no-op if
there's nothing ungrouped):

  python -m shared.src.run_matching
"""
import logging
import sys

from . import content_store, matching

log = logging.getLogger(__name__)


def _match_into_existing_groups(client, ungrouped: list) -> tuple[list, int, int]:
    """Returns (still_ungrouped, added, pending) -- items that found no
    home in an existing group are passed through unchanged for the
    brand-new-group clustering pass."""
    existing = content_store.get_confirmed_group_members(client)
    if not existing:
        return ungrouped, 0, 0

    groups: dict[str, list] = {}
    for member in existing:
        groups.setdefault(member["Group_ID"], []).append(member)

    still_ungrouped = []
    added, pending = 0, 0
    for item in ungrouped:
        best_group_id, best_score = None, 0.0
        for group_id, members in groups.items():
            if any(m["Platform"] == item["Platform"] for m in members):
                continue  # would put two items from the same platform in one group
            for member in members:
                score = matching.pair_score(item, member)
                if score > best_score:
                    best_score, best_group_id = score, group_id

        if best_group_id is not None and best_score >= matching.MIN_SUGGEST_SCORE:
            confirmed = best_score >= matching.AUTO_CONFIRM_SCORE
            content_store.add_members(
                client,
                best_group_id,
                [item["Content_ID"]],
                match_method="auto",
                match_confidence=round(best_score, 3),
                confirmed=confirmed,
            )
            # So a second late item in this same run can't also claim this
            # group's now-taken platform slot, and so group membership
            # stays visible for scoring subsequent items against it.
            groups[best_group_id].append(item)
            if confirmed:
                added += 1
            else:
                pending += 1
        else:
            still_ungrouped.append(item)

    return still_ungrouped, added, pending


def match_items(client, items: list, updated_by: str = "auto_match") -> dict:
    """Runs both matching passes over an explicit list of ungrouped items
    (already fetched by the caller -- this only performs the matching plus
    the BigQuery writes, so both run() below and every platform pipeline's
    own end-of-run sync share the exact same logic rather than each
    reimplementing it with the existing-groups pass missing from one of
    them). Returns counts for logging."""
    if not items:
        return {"added_existing": 0, "pending_existing": 0, "created": 0, "pending": 0}

    items, added_existing, pending_existing = _match_into_existing_groups(client, items)

    candidates = matching.find_candidate_groups(items)
    created, pending = 0, 0
    for candidate in candidates:
        content_store.create_group(
            client,
            content_ids=candidate["content_ids"],
            match_method="auto",
            match_confidence=candidate["confidence"],
            confirmed=candidate["auto_confirm"],
            updated_by=updated_by,
        )
        if candidate["auto_confirm"]:
            created += 1
        else:
            pending += 1

    return {
        "added_existing": added_existing,
        "pending_existing": pending_existing,
        "created": created,
        "pending": pending,
    }


def run() -> int:
    client = content_store.get_client()
    content_store.ensure_schema(client)

    items = content_store.get_ungrouped_items(client)
    if not items:
        log.info("No ungrouped content_items -- nothing to match.")
        return 0

    log.info("Matching %d ungrouped content_items...", len(items))
    stats = match_items(client, items)

    log.info(
        "Created %d new confirmed group(s) and %d new pending-review group(s); "
        "%d item(s) added to existing groups (%d confirmed, %d pending review).",
        stats["created"],
        stats["pending"],
        stats["added_existing"] + stats["pending_existing"],
        stats["added_existing"],
        stats["pending_existing"],
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

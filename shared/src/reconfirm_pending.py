"""
Re-scores every *auto*-matched group membership (Match_Method='auto') --
pending or already confirmed -- against its group's other members, using
live content_items data and whatever matching.py's pair_score() currently
computes. Never touches Match_Method='manual' links; a human's own
decision is never second-guessed by this script.

Why this exists at all: a membership's stored Match_Confidence is a
snapshot from whenever it was first suggested, computed under whatever
matching.py logic existed *at that time*. Naively comparing that stale
number against today's AUTO_CONFIRM_SCORE (which is exactly what an
earlier version of this script did) silently promotes matches that were
only ever plausible under an old, since-fixed formula. Confirmed live
(Aug 2026): a March 2026 Facebook/Instagram post and an August 2026
TikTok video -- 5 months apart -- ended up "Matched" this way, because
the stored confidence predated matching.py's hard date-proximity gate.
Re-running this against the live database found hundreds more of exactly
this class of bug (score recomputes to a flat 0.000 -- the date gate --
where the stored confidence had been well above threshold).

Applies the recomputed score:
  - below MIN_SUGGEST_SCORE  -> severed entirely (remove_member)
  - MIN_SUGGEST_SCORE .. AUTO_CONFIRM_SCORE -> left/set as pending review
  - AUTO_CONFIRM_SCORE or above -> confirmed

All scoring happens in memory first; the writes are two bulk queries
(one DELETE, one MERGE) rather than one query per membership -- the
first version issued a separate BigQuery query job per change, and at
the hundreds-of-bad-matches volumes this database actually had, each job
paying a few seconds of startup overhead made the whole run look hung.

Safe to run any time -- e.g. after any matching.py change, or just
periodically. A no-op for a membership whose recomputed score matches its
current state.

  python -m shared.src.reconfirm_pending
"""
import logging
import sys

from . import content_store, matching

log = logging.getLogger(__name__)


def run() -> int:
    client = content_store.get_client()

    all_members = content_store.get_all_group_members(client)
    groups: dict[str, list] = {}
    for member in all_members:
        groups.setdefault(member["Group_ID"], []).append(member)

    to_sever = []
    to_update = []
    demoted, promoted, unchanged = 0, 0, 0

    for group_id, members in groups.items():
        for member in members:
            if member["Match_Method"] != "auto":
                continue  # never touch a human-created link

            others = [m for m in members if m["Content_ID"] != member["Content_ID"]]
            score = round(matching.best_pairwise_score(member, others), 3)

            if score < matching.MIN_SUGGEST_SCORE:
                to_sever.append(member["Content_ID"])
                log.info(
                    "Will sever %s from group %s -- recomputed score %.3f no longer clears "
                    "MIN_SUGGEST_SCORE=%.2f (was Confirmed=%s).",
                    member["Content_ID"], group_id, score, matching.MIN_SUGGEST_SCORE, member["Confirmed"],
                )
                continue

            should_be_confirmed = score >= matching.AUTO_CONFIRM_SCORE
            if should_be_confirmed != member["Confirmed"] or score != member.get("Match_Confidence"):
                to_update.append(
                    {"Content_ID": member["Content_ID"], "Confirmed": should_be_confirmed, "Match_Confidence": score}
                )
                if should_be_confirmed and not member["Confirmed"]:
                    promoted += 1
                elif not should_be_confirmed and member["Confirmed"]:
                    demoted += 1
                    log.info(
                        "Will demote %s in group %s to pending review -- recomputed score %.3f no "
                        "longer clears AUTO_CONFIRM_SCORE=%.2f.",
                        member["Content_ID"], group_id, score, matching.AUTO_CONFIRM_SCORE,
                    )
                else:
                    unchanged += 1  # confidence value updated, confirm status unchanged
            else:
                unchanged += 1

    log.info(
        "Applying: %d severing, %d confirm-status/confidence updates...", len(to_sever), len(to_update)
    )
    content_store.bulk_remove_members(client, to_sever)
    content_store.bulk_set_membership_status(client, to_update)

    log.info(
        "Done. %d severed (no longer a plausible match), %d demoted to pending, "
        "%d newly confirmed, %d unchanged.",
        len(to_sever), demoted, promoted, unchanged,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

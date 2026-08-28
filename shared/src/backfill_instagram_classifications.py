"""
One-time import: applies the manual classifications already sitting in a
pre-existing spreadsheet-derived table (originally
project-6f3dedab-dbda-4261-90d.master_data.instagram -- every Instagram
video the user had already classified by Partnership/Content_Type before
this system existed) to the matching content in this system.

Reads from master_data.instagram_native rather than the original
Sheets-linked master_data.instagram directly: a BigQuery table backed by
a Google Sheet requires Drive-scoped credentials to query, and the gcloud
CLI's OAuth client is blocked by Google from ever requesting that scope
("This app is blocked" -- a sensitive-scope restriction gcloud itself
can't clear, not a bug in this script). instagram_native is a one-time
`CREATE TABLE ... AS SELECT * FROM master_data.instagram` snapshot taken
via the BigQuery web console (which already has full Drive access
through the normal browser sign-in), so this script and everything else
here only ever needs plain BigQuery scope.

Matches on Post_ID (-> Content_ID "instagram:{Post_ID}"). Any Post_ID not
found in our own Instagram data (never synced, or since deleted) is
skipped and counted, not an error.

Classifying is done at the *group* level, same as every other
classification path in this system (webapp's Save/Apply All, the manual
grouping feature): if the Instagram post already belongs to a
cross-platform group, the whole group -- Facebook/YouTube/TikTok
cross-posts included -- gets classified, not just the Instagram row. An
Instagram post with no group yet gets a new one-member group created for
it. Any Partnership/Content_Type combination from the legacy table that
doesn't already exist in this system's partnerships/content-types
reference tables is created automatically.

  python -m shared.src.backfill_instagram_classifications
"""
import logging
import sys

from . import content_store

log = logging.getLogger(__name__)

LEGACY_TABLE = "project-6f3dedab-dbda-4261-90d.master_data.instagram_native"
UPDATED_BY = "legacy_instagram_backfill"


def run() -> int:
    client = content_store.get_client()
    content_store.ensure_schema(client)

    log.info("Reading legacy classifications from %s...", LEGACY_TABLE)
    rows = [
        dict(r)
        for r in client.query(
            f"""
            SELECT TRIM(Post_ID) AS Post_ID, TRIM(Partnership) AS Partnership,
              TRIM(IFNULL(Content_Type, '')) AS Content_Type
            FROM `{LEGACY_TABLE}`
            WHERE Partnership IS NOT NULL AND TRIM(Partnership) != ''
            """
        ).result()
    ]
    log.info("Found %d already-classified row(s) in the legacy table.", len(rows))
    if not rows:
        return 0

    seen_pairs = set()
    for r in rows:
        pair = (r["Partnership"], r["Content_Type"] or "Unclassified")
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        content_store.add_content_type(client, pair[0], pair[1])
    log.info("Ensured %d distinct Partnership/Content_Type combination(s) exist in this system.", len(seen_pairs))

    candidate_ids = [f"instagram:{r['Post_ID']}" for r in rows]
    known = content_store.get_platform_and_group_for_content_ids(client, candidate_ids)

    to_apply = []
    skipped = 0
    for r in rows:
        content_id = f"instagram:{r['Post_ID']}"
        if content_id not in known:
            skipped += 1
            continue
        to_apply.append(
            {
                "content_id": content_id,
                "group_id": known[content_id]["Group_ID"],
                "partnership": r["Partnership"],
                "content_type": r["Content_Type"] or "Unclassified",
            }
        )
    log.info(
        "%d matched our Instagram data (will be classified), %d not found here (never synced or "
        "since deleted) -- skipped.", len(to_apply), skipped,
    )
    if not to_apply:
        return 0

    new_rows = [r for r in to_apply if not r["group_id"]]
    existing_rows = [r for r in to_apply if r["group_id"]]

    if new_rows:
        new_group_ids = content_store.bulk_create_classified_groups(
            client,
            [{"content_id": r["content_id"], "partnership": r["partnership"], "content_type": r["content_type"]} for r in new_rows],
            updated_by=UPDATED_BY,
        )
        for r, gid in zip(new_rows, new_group_ids):
            r["group_id"] = gid

    if existing_rows:
        content_store.bulk_set_classifications(
            client,
            [{"Group_ID": r["group_id"], "Partnership": r["partnership"], "Content_Type": r["content_type"]} for r in existing_rows],
            updated_by=UPDATED_BY,
        )

    # Propagate to every member's own platform *_classifications table --
    # not just Instagram's -- so a cross-posted Facebook/YouTube/TikTok
    # copy of the same content gets the same classification too.
    member_map = content_store.bulk_member_platform_ids(client, [r["group_id"] for r in existing_rows])
    by_platform: dict = {}
    for r in to_apply:
        members = member_map.get(r["group_id"]) or [("Instagram", r["content_id"].split(":", 1)[1])]
        for plat, post_id in members:
            by_platform.setdefault(plat, []).append(
                {"post_id": post_id, "partnership": r["partnership"], "content_type": r["content_type"]}
            )
    for plat, items in by_platform.items():
        content_store.propagate_bulk_classifications(client, plat, items, updated_by=UPDATED_BY)

    log.info(
        "Done. Classified %d group(s) from the legacy table (%d newly created one-member groups, "
        "%d already-existing groups updated).", len(to_apply), len(new_rows), len(existing_rows),
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

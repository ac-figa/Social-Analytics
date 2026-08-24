"""
Full rebuild: wipes all cross-platform grouping state (content_groups,
content_group_members) and re-runs matching from a clean slate, instead
of continuing to patch a matching history built up before the date-gate,
duration-signal, and bulk-reconciliation fixes existed.

Why a rebuild beats patching here: the historical damage wasn't just bad
individual matches (reconfirm_pending.py already fixes those) -- some
real matches were split across two *separate* groups that each formed
correctly on their own, before either was ever compared against the
other (e.g. TikTok+YouTube matched each other, Facebook+Instagram
matched each other, but nothing ever checked the two groups against each
other). A clean rebuild sidesteps that entirely: matching.find_candidate_groups()
clusters every ungrouped item across every platform in one pass, so a
4-way match converges into one group directly -- confirmed against a
real case (score 0.79 between the two group's members, well above
AUTO_CONFIRM_SCORE) that a "compare existing groups pairwise" patch would
have needed a whole separate mechanism to catch, and even then only for
this one historical case, not future ones shaped the same way.

Safe to run: content_items (the raw synced data) is never touched, and
every classification you've already made survives independently in each
platform's own *_classifications table -- this reads those back and
reapplies them to whatever new group each piece of content lands in, so
no classification work is lost. If nothing has been classified yet, this
step is just a no-op.

  python -m shared.src.rebuild_groups
"""
import logging
import sys

from . import config, content_store, run_matching

log = logging.getLogger(__name__)


def _load_existing_classifications(client) -> dict:
    """{Content_ID: (Partnership, Content_Type)} pulled from each
    platform's own *_classifications table -- the durable source of
    truth that survives content_store.truncate_groups()."""
    result = {}
    for platform, p in config.PLATFORM_CONFIG.items():
        table_ref = f"{config.BQ_PROJECT_ID}.{p['dataset']}.{p['classifications_table']}"
        query = f"""
        SELECT {p['id_column']} AS ID, Partnership, Content_Type
        FROM `{table_ref}`
        WHERE Partnership IS NOT NULL AND Partnership != 'Unclassified'
        """
        try:
            rows = list(client.query(query).result())
        except Exception as e:  # noqa: BLE001 -- a missing/empty table shouldn't abort the rebuild
            log.warning("Could not read %s (skipping): %s", table_ref, e)
            continue
        for r in rows:
            content_id = f"{platform.lower()}:{r['ID']}"
            result[content_id] = (r["Partnership"], r["Content_Type"])
    return result


def run() -> int:
    client = content_store.get_client()
    content_store.ensure_schema(client)

    log.info("Reading existing classifications before wiping groups...")
    old_classifications = _load_existing_classifications(client)
    log.info("Found %d already-classified content item(s) to preserve.", len(old_classifications))

    log.info("Wiping all group/matching state (content_items is untouched)...")
    content_store.truncate_groups(client)

    log.info("Rebuilding groups from scratch using current matching logic...")
    exit_code = run_matching.run()
    if exit_code != 0:
        return exit_code

    log.info("Reapplying preserved classifications to the rebuilt groups...")
    reapplied = content_store.reapply_classifications(client, old_classifications)
    log.info("Done. Reapplied classifications to %d group(s).", reapplied)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

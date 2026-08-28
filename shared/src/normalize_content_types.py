"""
One-time cleanup: merges content-type duplicates that differ only in
whitespace around "/" (e.g. "Skit/Educational" vs "Skit / Educational")
into one canonical spelling -- confirmed live (Aug 2026) on Caffe Borbone,
which had both. content_store.normalize_content_type() now applies this
same normalization on every future write (set_classification,
bulk_set_classifications, create_group, bulk_create_classified_groups,
add_content_type, propagate_bulk_classifications), so this script only
needs to run once for whatever inconsistent data already exists.

Rewrites Content_Type wherever a copy of it is stored: content_groups,
partnership_content_types, and each platform's own *_classifications
table (Instagram/Facebook/YouTube/TikTok) -- a group's classification is
propagated to all of these, so a stale variant left in any one of them
would keep resurfacing.

  python -m shared.src.normalize_content_types
"""
import logging
import sys

from google.cloud import bigquery

from . import config, content_store

log = logging.getLogger(__name__)


def _rewrite(client: bigquery.Client, table_ref: str, partnership: str, variant: str, canonical: str) -> None:
    client.query(
        f"""
        UPDATE `{table_ref}`
        SET Content_Type = @canonical
        WHERE Partnership = @partnership AND Content_Type = @variant
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("canonical", "STRING", canonical),
                bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
                bigquery.ScalarQueryParameter("variant", "STRING", variant),
            ]
        ),
    ).result()


def run() -> int:
    client = content_store.get_client()

    partnership_content_types_ref = (
        f"{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.{content_store.PARTNERSHIP_CONTENT_TYPES_TABLE}"
    )
    content_groups_ref = f"{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.{content_store.CONTENT_GROUPS_TABLE}"

    rows = [
        dict(r)
        for r in client.query(f"SELECT Partnership, Content_Type FROM `{partnership_content_types_ref}`").result()
    ]

    # {(Partnership, normalized Content_Type): {raw variants}}
    variants: dict = {}
    for r in rows:
        key = (r["Partnership"], content_store.normalize_content_type(r["Content_Type"]))
        variants.setdefault(key, set()).add(r["Content_Type"])

    to_fix = {k: v for k, v in variants.items() if len(v) > 1}
    if not to_fix:
        log.info("No content-type spacing duplicates found -- nothing to do.")
        return 0

    log.info("Found %d partnership/content-type combination(s) with duplicate spellings.", len(to_fix))

    for (partnership, canonical), raw_variants in to_fix.items():
        stale_variants = [v for v in raw_variants if v != canonical]
        log.info(
            "%s -- merging %s into %r", partnership,
            ", ".join(repr(v) for v in stale_variants), canonical,
        )
        for variant in stale_variants:
            _rewrite(client, content_groups_ref, partnership, variant, canonical)

            for p in config.PLATFORM_CONFIG.values():
                table_ref = f"{config.BQ_PROJECT_ID}.{p['dataset']}.{p['classifications_table']}"
                _rewrite(client, table_ref, partnership, variant, canonical)

            client.query(
                f"""
                DELETE FROM `{partnership_content_types_ref}`
                WHERE Partnership = @partnership AND Content_Type = @variant
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
                        bigquery.ScalarQueryParameter("variant", "STRING", variant),
                    ]
                ),
            ).result()

        # Guarantees the canonical spelling has exactly one row in
        # partnership_content_types (idempotent -- a no-op if it's
        # already there from one of the variants matching it exactly).
        content_store.add_content_type(client, partnership, canonical)

    log.info("Done. Merged %d duplicate content-type combination(s).", len(to_fix))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())

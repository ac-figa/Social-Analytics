"""
BigQuery persistence for the cross-platform content layer.

Three tables, additive on top of each platform pipeline's own tables
(instagram_master, facebook_master, ...) -- those keep their full detailed
schema and are the source of truth for platform-specific fields; this
layer only carries the normalized subset needed to match content across
platforms and report on it per-partnership.

  content_items          One row per (platform, platform_post_id). Upserted
                          by every platform pipeline after its own ingest.
  content_groups          One row per real-world piece of content -- what
                          Partnership/Content_Type actually get set on.
  content_group_members   Which content_items belong to which group, and
                          whether that link was manually confirmed.

Same upsert pattern as the Instagram pipeline's bigquery_store.py:
truncate-and-load a staging table, then MERGE.
"""
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone

from google.cloud import bigquery

from . import config

log = logging.getLogger(__name__)

_SLASH_SPACING_RE = re.compile(r"\s*/\s*")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_content_type(content_type: str) -> str:
    """Collapses whitespace and normalizes spacing around '/' so
    "Skit/Educational", "Skit / Educational", and "Skit /Educational" are
    all treated as the same content type. Confirmed live (Aug 2026) that
    free-text entry -- both manual and via the legacy classifications
    backfill -- had produced two separate partnership_content_types rows
    for what was meant to be one type, purely from slash-spacing
    inconsistency. Applied at every write path that touches Content_Type
    (set_classification, bulk_set_classifications, create_group,
    bulk_create_classified_groups, add_content_type,
    propagate_bulk_classifications) so the same normalization can never be
    skipped by calling one of them directly."""
    if not content_type:
        return content_type
    return _WHITESPACE_RE.sub(" ", _SLASH_SPACING_RE.sub(" / ", content_type.strip()))

CONTENT_ITEMS_TABLE = "content_items"
CONTENT_ITEMS_STAGING_TABLE = "content_items_staging"
CONTENT_GROUPS_TABLE = "content_groups"
CONTENT_GROUP_MEMBERS_TABLE = "content_group_members"

CONTENT_ITEMS_SCHEMA = [
    bigquery.SchemaField("Content_ID", "STRING", mode="REQUIRED"),  # "{platform}:{platform_post_id}"
    bigquery.SchemaField("Platform", "STRING", mode="REQUIRED"),  # Instagram / YouTube / TikTok / Facebook
    bigquery.SchemaField("Platform_Post_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Account_Username", "STRING"),
    bigquery.SchemaField("Caption", "STRING"),
    bigquery.SchemaField("Publish_Date", "TIMESTAMP"),
    bigquery.SchemaField("Permalink", "STRING"),
    bigquery.SchemaField("Post_Type", "STRING"),
    bigquery.SchemaField("Thumbnail_URL", "STRING"),
    bigquery.SchemaField("Duration", "FLOAT64"),  # seconds; NULL for Instagram (see matching.py)
    bigquery.SchemaField("Views", "INT64"),
    bigquery.SchemaField("Likes", "INT64"),
    bigquery.SchemaField("Comments", "INT64"),
    bigquery.SchemaField("Shares", "INT64"),
    bigquery.SchemaField("Saves", "INT64"),
    bigquery.SchemaField("API_Status", "STRING"),  # Active / Deleted_or_Unavailable
    bigquery.SchemaField("Last_Synced_At", "TIMESTAMP"),
]

CONTENT_GROUPS_SCHEMA = [
    bigquery.SchemaField("Group_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Partnership", "STRING"),
    bigquery.SchemaField("Content_Type", "STRING"),
    bigquery.SchemaField("Notes", "STRING"),
    bigquery.SchemaField("Created_At", "TIMESTAMP"),
    bigquery.SchemaField("Updated_At", "TIMESTAMP"),
    bigquery.SchemaField("Updated_By", "STRING"),
]

CONTENT_GROUP_MEMBERS_SCHEMA = [
    bigquery.SchemaField("Group_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Content_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Match_Method", "STRING"),  # auto / manual
    bigquery.SchemaField("Match_Confidence", "FLOAT64"),  # NULL for manual links
    bigquery.SchemaField("Confirmed", "BOOL"),  # False = auto-suggested, awaiting review
    bigquery.SchemaField("Added_At", "TIMESTAMP"),
]

# Reference tables for the classification dashboard (webapp/) -- lets it
# offer dropdowns instead of free text, and keeps the partnership/content
# type vocabulary consistent across every content_group. content_groups
# doesn't enforce a foreign key against these (BigQuery has no FK
# constraints); the dashboard is what keeps the two in sync.
PARTNERSHIPS_TABLE = "partnerships"
PARTNERSHIP_CONTENT_TYPES_TABLE = "partnership_content_types"

PARTNERSHIPS_SCHEMA = [
    bigquery.SchemaField("Partnership", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Created_At", "TIMESTAMP"),
    # Set the first time a share link is generated for this partnership
    # (see get_or_create_share_token) -- an unguessable token rather than
    # the partnership name itself, so a brand's link doesn't also expose
    # every other partnership's name by pattern-guessing. NULL until
    # then; stays the same across regenerated report views so a link
    # once shared keeps working.
    bigquery.SchemaField("Share_Token", "STRING"),
]

PARTNERSHIP_CONTENT_TYPES_SCHEMA = [
    bigquery.SchemaField("Partnership", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Content_Type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Created_At", "TIMESTAMP"),
]

# Daily follower/subscriber snapshots for the media kit -- one row per
# account per day, so "latest" is just the newest Snapshot_Date and
# growth-over-time comes for free. Lives in the shared layer (rather than
# each pipeline's own dataset) because every platform's shape here is
# identical, unlike each pipeline's platform-specific master schema.
ACCOUNT_STATS_TABLE = "account_stats"

ACCOUNT_STATS_SCHEMA = [
    bigquery.SchemaField("Platform", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Account_Username", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Account_ID", "STRING"),
    bigquery.SchemaField("Followers", "INT64"),
    bigquery.SchemaField("Snapshot_Date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("Captured_At", "TIMESTAMP"),
]

# Stories (Instagram/Facebook/TikTok) aren't worth pulling from each
# platform's API -- they expire in 24h and the volume posted under a
# partnership is low -- so this is manual entry only, no pipeline writes
# here. Field names deliberately mirror content_items' conventions
# (Caption, Publish_Date, Views/Likes/Shares, Partnership, Content_Type)
# rather than a bespoke naming scheme, so a story can be aggregated
# alongside regular posts wherever that makes sense (e.g. the
# partnership report). Sticker_Taps and Replies have no content_items
# equivalent -- they're story-specific engagement types.
STORIES_TABLE = "stories"

STORIES_SCHEMA = [
    bigquery.SchemaField("Story_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Platform", "STRING"),
    bigquery.SchemaField("Account_Username", "STRING"),
    bigquery.SchemaField("Caption", "STRING"),
    bigquery.SchemaField("Publish_Date", "DATE"),
    bigquery.SchemaField("Views", "INT64"),
    bigquery.SchemaField("Likes", "INT64"),
    bigquery.SchemaField("Shares", "INT64"),
    bigquery.SchemaField("Sticker_Taps", "INT64"),
    bigquery.SchemaField("Replies", "INT64"),
    bigquery.SchemaField("Tagged", "STRING"),
    bigquery.SchemaField("Partnership", "STRING"),
    bigquery.SchemaField("Content_Type", "STRING"),
    bigquery.SchemaField("Created_At", "TIMESTAMP"),
    bigquery.SchemaField("Updated_At", "TIMESTAMP"),
]

_CONTENT_ITEMS_UPDATE_COLUMNS = [f.name for f in CONTENT_ITEMS_SCHEMA if f.name != "Content_ID"]


def content_id(platform: str, platform_post_id: str) -> str:
    return f"{platform.lower()}:{platform_post_id}"


def get_client() -> bigquery.Client:
    return bigquery.Client(project=config.BQ_PROJECT_ID)


def _table_ref(name: str) -> str:
    return f"{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.{name}"


def ensure_schema(client: bigquery.Client) -> None:
    dataset_ref = bigquery.DatasetReference(config.BQ_PROJECT_ID, config.SHARED_BQ_DATASET)
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        log.info("Creating dataset %s", config.SHARED_BQ_DATASET)
        client.create_dataset(bigquery.Dataset(dataset_ref))

    for name, schema in (
        (CONTENT_ITEMS_TABLE, CONTENT_ITEMS_SCHEMA),
        (CONTENT_GROUPS_TABLE, CONTENT_GROUPS_SCHEMA),
        (CONTENT_GROUP_MEMBERS_TABLE, CONTENT_GROUP_MEMBERS_SCHEMA),
        (PARTNERSHIPS_TABLE, PARTNERSHIPS_SCHEMA),
        (PARTNERSHIP_CONTENT_TYPES_TABLE, PARTNERSHIP_CONTENT_TYPES_SCHEMA),
        (ACCOUNT_STATS_TABLE, ACCOUNT_STATS_SCHEMA),
        (STORIES_TABLE, STORIES_SCHEMA),
    ):
        table_id = _table_ref(name)
        try:
            client.get_table(table_id)
        except Exception:
            log.info("Creating table %s", table_id)
            client.create_table(bigquery.Table(table_id, schema=schema))


def _content_items_update_expr(col: str) -> str:
    """Instagram's to_content_item() always produces Duration=NULL every
    run -- the Graph API never returns one (see
    instagramanalyticspipeline/docs/API_NOTES.md). Without this
    carve-out, every normal Instagram pipeline run would stomp the value
    shared/src/backfill_instagram_duration.py wrote in right back to
    NULL. Every other platform's real Duration still updates normally."""
    if col == "Duration":
        return "T.Duration = CASE WHEN S.Platform = 'Instagram' THEN T.Duration ELSE S.Duration END"
    return f"T.{col} = S.{col}"


def upsert_content_items(client: bigquery.Client, rows: list) -> None:
    """rows: list of dicts matching CONTENT_ITEMS_SCHEMA. Called by every
    platform pipeline after it builds its own platform-specific rows."""
    if not rows:
        log.info("No content items to upsert.")
        return

    staging_id = _table_ref(CONTENT_ITEMS_STAGING_TABLE)
    client.create_table(bigquery.Table(staging_id, schema=CONTENT_ITEMS_SCHEMA), exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        schema=CONTENT_ITEMS_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(rows, staging_id, job_config=job_config).result()

    update_clause = ", ".join(_content_items_update_expr(c) for c in _CONTENT_ITEMS_UPDATE_COLUMNS)
    insert_columns = [f.name for f in CONTENT_ITEMS_SCHEMA]
    insert_values = ", ".join(f"S.{c}" for c in insert_columns)

    merge_sql = f"""
    MERGE `{_table_ref(CONTENT_ITEMS_TABLE)}` T
    USING `{staging_id}` S
    ON T.Content_ID = S.Content_ID
    WHEN MATCHED THEN UPDATE SET {update_clause}
    WHEN NOT MATCHED THEN INSERT ({", ".join(insert_columns)})
      VALUES ({insert_values})
    """
    client.query(merge_sql).result()
    log.info("Upserted %d rows into %s", len(rows), CONTENT_ITEMS_TABLE)


def get_ungrouped_items(client: bigquery.Client) -> list:
    """Active content_items with no row in content_group_members at all --
    candidates for matching (auto or manual)."""
    query = f"""
    SELECT ci.Content_ID, ci.Platform, ci.Caption, ci.Publish_Date, ci.Duration
    FROM `{_table_ref(CONTENT_ITEMS_TABLE)}` ci
    LEFT JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m
      ON ci.Content_ID = m.Content_ID
    WHERE m.Content_ID IS NULL AND ci.API_Status = 'Active'
    """
    return [dict(r) for r in client.query(query).result()]


def get_confirmed_group_members(client: bigquery.Client) -> list:
    """Every confirmed content_group_members row, with the fields
    matching.pair_score needs -- used to test whether a newly-ungrouped
    item (e.g. from a platform pipeline that only just started existing)
    actually belongs in a group formed by an earlier matching run, since
    get_ungrouped_items() only ever sees items with zero group membership
    and would otherwise never reconsider them."""
    query = f"""
    SELECT m.Group_ID, ci.Content_ID, ci.Platform, ci.Caption, ci.Publish_Date, ci.Duration
    FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
    WHERE m.Confirmed = TRUE
    """
    return [dict(r) for r in client.query(query).result()]


def truncate_groups(client: bigquery.Client) -> None:
    """Wipes ALL grouping/matching state (content_group_members and
    content_groups) for a full rebuild -- see rebuild_groups.py. Never
    touches content_items (the raw synced data) or any platform's own
    *_classifications table, which is exactly what makes the rebuild safe:
    every classification you've made survives independently there."""
    client.query(f"TRUNCATE TABLE `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`").result()
    client.query(f"TRUNCATE TABLE `{_table_ref(CONTENT_GROUPS_TABLE)}`").result()


def reapply_classifications(client: bigquery.Client, content_id_classifications: dict) -> int:
    """content_id_classifications: {Content_ID: (Partnership, Content_Type)}.
    After a rebuild, every new content_group starts Unclassified even
    though the underlying content was already classified -- that data
    lives durably per-platform, untouched by truncate_groups(). This
    looks up each new group's members against the preserved mapping and
    sets the group's Partnership/Content_Type to match wherever found.
    Returns how many groups got reclassified this way."""
    if not content_id_classifications:
        return 0

    all_members = get_all_group_members(client)
    groups: dict[str, list] = {}
    for m in all_members:
        groups.setdefault(m["Group_ID"], []).append(m)

    updates = []
    for group_id, members in groups.items():
        for m in members:
            found = content_id_classifications.get(m["Content_ID"])
            if found:
                updates.append({"Group_ID": group_id, "Partnership": found[0], "Content_Type": found[1]})
                break

    if not updates:
        return 0

    staging_id = _table_ref("content_groups_reclassify_staging")
    staging_schema = [
        bigquery.SchemaField("Group_ID", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Partnership", "STRING"),
        bigquery.SchemaField("Content_Type", "STRING"),
    ]
    client.create_table(bigquery.Table(staging_id, schema=staging_schema), exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        schema=staging_schema,
        write_disposition="WRITE_TRUNCATE",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(updates, staging_id, job_config=job_config).result()

    merge_sql = f"""
    MERGE `{_table_ref(CONTENT_GROUPS_TABLE)}` T
    USING `{staging_id}` S
    ON T.Group_ID = S.Group_ID
    WHEN MATCHED THEN UPDATE SET T.Partnership = S.Partnership, T.Content_Type = S.Content_Type,
      T.Updated_At = CURRENT_TIMESTAMP(), T.Updated_By = 'rebuild_groups'
    """
    client.query(merge_sql).result()
    return len(updates)


def merge_groups(client: bigquery.Client, keep_group_id: str, absorb_group_id: str) -> None:
    """Moves every member of absorb_group_id into keep_group_id and
    deletes the now-empty absorb_group_id row. Caller decides whether
    merging is appropriate (platform overlap, classification conflicts,
    score threshold) -- this just performs it."""
    client.query(
        f"""
        UPDATE `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
        SET Group_ID = @keep_group_id
        WHERE Group_ID = @absorb_group_id
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("keep_group_id", "STRING", keep_group_id),
                bigquery.ScalarQueryParameter("absorb_group_id", "STRING", absorb_group_id),
            ]
        ),
    ).result()
    client.query(
        f"DELETE FROM `{_table_ref(CONTENT_GROUPS_TABLE)}` WHERE Group_ID = @absorb_group_id",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("absorb_group_id", "STRING", absorb_group_id)]
        ),
    ).result()


def get_all_group_members(client: bigquery.Client) -> list:
    """Every content_group_members row (any Confirmed/Match_Method status),
    with the fields matching.pair_score needs -- the full dataset
    reconfirm_pending.py and reaudit_confirmed_matches.py group by
    Group_ID in Python to re-score each auto-matched membership against
    its group's *other* members using current live data and the current
    matching.py formula, rather than trusting a stored Match_Confidence
    that may have been computed under an older version of either."""
    query = f"""
    SELECT m.Group_ID, m.Content_ID, m.Confirmed, m.Match_Method,
      ci.Platform, ci.Caption, ci.Publish_Date, ci.Duration
    FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
    """
    return [dict(r) for r in client.query(query).result()]


def set_membership_status(
    client: bigquery.Client, group_id: str, content_id: str, confirmed: bool, match_confidence: float
) -> None:
    """Updates an existing membership's Confirmed flag and
    Match_Confidence in place -- used by the reconciliation scripts to
    record a freshly-recomputed score, promoting/demoting as needed."""
    query = f"""
    UPDATE `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
    SET Confirmed = @confirmed, Match_Confidence = @match_confidence
    WHERE Group_ID = @group_id AND Content_ID = @content_id
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("group_id", "STRING", group_id),
                bigquery.ScalarQueryParameter("content_id", "STRING", content_id),
                bigquery.ScalarQueryParameter("confirmed", "BOOL", confirmed),
                bigquery.ScalarQueryParameter("match_confidence", "FLOAT64", match_confidence),
            ]
        ),
    ).result()


def bulk_remove_members(client: bigquery.Client, content_ids: list) -> None:
    """Same effect as calling remove_member() once per ID, but as a single
    DELETE -- used by reconfirm_pending.py, which can have hundreds of
    memberships to sever in one run; issuing that many separate query
    jobs each pay BigQuery's per-job startup overhead (a few seconds even
    for a trivial single-row DML statement), which is what made that
    script feel hung rather than just working. A Content_ID belongs to at
    most one group at a time, so this is unambiguous without needing
    Group_ID too."""
    if not content_ids:
        return
    query = f"""
    DELETE FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
    WHERE Content_ID IN UNNEST(@content_ids)
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("content_ids", "STRING", content_ids)]
        ),
    ).result()


def bulk_set_membership_status(client: bigquery.Client, updates: list) -> None:
    """updates: [{"Content_ID":..., "Confirmed":..., "Match_Confidence":...}].
    Bulk equivalent of set_membership_status() -- see bulk_remove_members()
    for why this matters at the volumes reconfirm_pending.py deals with."""
    if not updates:
        return
    staging_id = _table_ref("content_group_members_status_staging")
    staging_schema = [
        bigquery.SchemaField("Content_ID", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Confirmed", "BOOL"),
        bigquery.SchemaField("Match_Confidence", "FLOAT64"),
    ]
    client.create_table(bigquery.Table(staging_id, schema=staging_schema), exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        schema=staging_schema,
        write_disposition="WRITE_TRUNCATE",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(updates, staging_id, job_config=job_config).result()

    merge_sql = f"""
    MERGE `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` T
    USING `{staging_id}` S
    ON T.Content_ID = S.Content_ID
    WHEN MATCHED THEN UPDATE SET T.Confirmed = S.Confirmed, T.Match_Confidence = S.Match_Confidence
    """
    client.query(merge_sql).result()


def create_group(
    client: bigquery.Client,
    content_ids: list,
    partnership: str = "Unclassified",
    content_type: str = "Unclassified",
    match_method: str = "manual",
    match_confidence: float = None,
    confirmed: bool = True,
    updated_by: str = "system",
) -> str:
    """Creates a new content_group and links content_ids to it. Returns the
    new Group_ID. Each content_id must not already belong to a group --
    move it out first (see move_member) if it does."""
    group_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    content_type = normalize_content_type(content_type)

    client.query(
        f"""
        INSERT INTO `{_table_ref(CONTENT_GROUPS_TABLE)}`
          (Group_ID, Partnership, Content_Type, Notes, Created_At, Updated_At, Updated_By)
        VALUES (@group_id, @partnership, @content_type, NULL, @now, @now, @updated_by)
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("group_id", "STRING", group_id),
                bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
                bigquery.ScalarQueryParameter("content_type", "STRING", content_type),
                bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
                bigquery.ScalarQueryParameter("updated_by", "STRING", updated_by),
            ]
        ),
    ).result()

    add_members(client, group_id, content_ids, match_method, match_confidence, confirmed)
    return group_id


def add_members(
    client: bigquery.Client,
    group_id: str,
    content_ids: list,
    match_method: str,
    match_confidence: float,
    confirmed: bool,
) -> None:
    """Uses a DML INSERT rather than the streaming insert_rows_json API --
    streamed rows sit in BigQuery's streaming buffer for up to ~90 minutes,
    during which any UPDATE/DELETE against them is rejected (confirmed live,
    Aug 2026: accepting a pending match minutes after it was created failed
    with "would affect rows in the streaming buffer"). DML-inserted rows
    have no such delay, so confirm_membership/remove_member/set_membership_status
    work on a just-created membership immediately."""
    if not content_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    query = f"""
    INSERT INTO `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
      (Group_ID, Content_ID, Match_Method, Match_Confidence, Confirmed, Added_At)
    SELECT @group_id, content_id, @match_method, @match_confidence, @confirmed, @now
    FROM UNNEST(@content_ids) AS content_id
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("group_id", "STRING", group_id),
                bigquery.ArrayQueryParameter("content_ids", "STRING", content_ids),
                bigquery.ScalarQueryParameter("match_method", "STRING", match_method),
                bigquery.ScalarQueryParameter("match_confidence", "FLOAT64", match_confidence),
                bigquery.ScalarQueryParameter("confirmed", "BOOL", confirmed),
                bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
            ]
        ),
    ).result()


def bulk_create_classified_groups(client: bigquery.Client, items: list, updated_by: str) -> list:
    """items: [{"content_id":..., "partnership":..., "content_type":...}].
    Creates one brand-new one-member group per item -- a staging-table
    load plus two INSERT...SELECT statements, rather than one INSERT pair
    per group, for the same reason bulk_set_membership_status batches its
    writes: N sequential query jobs each pay BigQuery's per-job startup
    overhead, which is exactly what the webapp's "Apply All" bulk-classify
    button exists to avoid when a human just filled in a whole page of
    partnerships at once. Returns the new Group_ID for each item, same
    order as `items`. A load job (unlike insert_rows_json) never creates a
    streaming buffer, so the immediate INSERT...SELECT that follows -- and
    any later UPDATE/DELETE on these rows -- isn't blocked."""
    if not items:
        return []
    now = datetime.now(timezone.utc).isoformat()
    group_ids = [str(uuid.uuid4()) for _ in items]

    groups_staging = _table_ref("content_groups_bulk_staging")
    groups_schema = [
        bigquery.SchemaField("Group_ID", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Partnership", "STRING"),
        bigquery.SchemaField("Content_Type", "STRING"),
        bigquery.SchemaField("Created_At", "TIMESTAMP"),
        bigquery.SchemaField("Updated_At", "TIMESTAMP"),
        bigquery.SchemaField("Updated_By", "STRING"),
    ]
    client.create_table(bigquery.Table(groups_staging, schema=groups_schema), exists_ok=True)
    client.load_table_from_json(
        [
            {
                "Group_ID": gid, "Partnership": it["partnership"], "Content_Type": normalize_content_type(it["content_type"]),
                "Created_At": now, "Updated_At": now, "Updated_By": updated_by,
            }
            for gid, it in zip(group_ids, items)
        ],
        groups_staging,
        job_config=bigquery.LoadJobConfig(
            schema=groups_schema, write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ),
    ).result()
    client.query(f"""
        INSERT INTO `{_table_ref(CONTENT_GROUPS_TABLE)}`
          (Group_ID, Partnership, Content_Type, Notes, Created_At, Updated_At, Updated_By)
        SELECT Group_ID, Partnership, Content_Type, NULL, Created_At, Updated_At, Updated_By
        FROM `{groups_staging}`
    """).result()

    members_staging = _table_ref("content_group_members_bulk_staging")
    members_schema = [
        bigquery.SchemaField("Group_ID", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Content_ID", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Match_Method", "STRING"),
        bigquery.SchemaField("Confirmed", "BOOL"),
        bigquery.SchemaField("Added_At", "TIMESTAMP"),
    ]
    client.create_table(bigquery.Table(members_staging, schema=members_schema), exists_ok=True)
    client.load_table_from_json(
        [
            {"Group_ID": gid, "Content_ID": it["content_id"], "Match_Method": "manual", "Confirmed": True, "Added_At": now}
            for gid, it in zip(group_ids, items)
        ],
        members_staging,
        job_config=bigquery.LoadJobConfig(
            schema=members_schema, write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ),
    ).result()
    client.query(f"""
        INSERT INTO `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
          (Group_ID, Content_ID, Match_Method, Match_Confidence, Confirmed, Added_At)
        SELECT Group_ID, Content_ID, Match_Method, NULL, Confirmed, Added_At
        FROM `{members_staging}`
    """).result()

    return group_ids


def bulk_set_classifications(client: bigquery.Client, updates: list, updated_by: str) -> None:
    """updates: [{"Group_ID":..., "Partnership":..., "Content_Type":...}].
    Bulk equivalent of set_classification() -- see bulk_create_classified_groups()
    for why this matters for the webapp's "Apply All" button."""
    if not updates:
        return
    for u in updates:
        u["Content_Type"] = normalize_content_type(u["Content_Type"])
    staging_id = _table_ref("content_groups_classify_staging")
    staging_schema = [
        bigquery.SchemaField("Group_ID", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Partnership", "STRING"),
        bigquery.SchemaField("Content_Type", "STRING"),
    ]
    client.create_table(bigquery.Table(staging_id, schema=staging_schema), exists_ok=True)
    client.load_table_from_json(
        updates, staging_id,
        job_config=bigquery.LoadJobConfig(
            schema=staging_schema, write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ),
    ).result()
    client.query(
        f"""
        MERGE `{_table_ref(CONTENT_GROUPS_TABLE)}` T
        USING `{staging_id}` S
        ON T.Group_ID = S.Group_ID
        WHEN MATCHED THEN UPDATE SET T.Partnership = S.Partnership, T.Content_Type = S.Content_Type,
          T.Updated_At = CURRENT_TIMESTAMP(), T.Updated_By = @updated_by
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("updated_by", "STRING", updated_by)]
        ),
    ).result()


def bulk_member_platform_ids(client: bigquery.Client, group_ids: list) -> dict:
    """{Group_ID: [(Platform, Platform_Post_ID), ...]} for every member of
    every group in group_ids, in one query -- used to propagate a bulk
    classification change to each member's own platform *_classifications
    table without querying per-group."""
    if not group_ids:
        return {}
    query = f"""
    SELECT m.Group_ID, ci.Platform, ci.Platform_Post_ID
    FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
    WHERE m.Group_ID IN UNNEST(@group_ids)
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("group_ids", "STRING", group_ids)]
        ),
    ).result()
    result: dict = {}
    for r in rows:
        result.setdefault(r["Group_ID"], []).append((r["Platform"], r["Platform_Post_ID"]))
    return result


def propagate_bulk_classifications(client: bigquery.Client, platform: str, items: list, updated_by: str) -> None:
    """items: [{"post_id":..., "partnership":..., "content_type":...}].
    Writes a batch of classifications into one platform's own
    *_classifications table (instagram_master's reporting surface, not
    just content_groups) via a staging-table load plus one MERGE, instead
    of one MERGE per item. Shared by the webapp's "Apply All" button and
    any bulk-classification backfill script (e.g. importing a legacy
    spreadsheet's classifications) -- both need the exact same
    group-classified-content-propagates-to-every-member's-own-platform-table
    behavior that content_store.set_classification() alone doesn't cover."""
    if not items:
        return
    p = config.PLATFORM_CONFIG[platform]
    table_ref = f"{config.BQ_PROJECT_ID}.{p['dataset']}.{p['classifications_table']}"
    id_column = p["id_column"]
    staging_id = _table_ref(f"{platform.lower()}_classify_bulk_staging")
    staging_schema = [
        bigquery.SchemaField(id_column, "STRING", mode="REQUIRED"),
        bigquery.SchemaField("Partnership", "STRING"),
        bigquery.SchemaField("Content_Type", "STRING"),
    ]
    client.create_table(bigquery.Table(staging_id, schema=staging_schema), exists_ok=True)
    client.load_table_from_json(
        [{id_column: it["post_id"], "Partnership": it["partnership"], "Content_Type": normalize_content_type(it["content_type"])} for it in items],
        staging_id,
        job_config=bigquery.LoadJobConfig(
            schema=staging_schema, write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ),
    ).result()
    client.query(
        f"""
        MERGE `{table_ref}` T
        USING `{staging_id}` S
        ON T.{id_column} = S.{id_column}
        WHEN MATCHED THEN UPDATE SET
          Partnership = S.Partnership, Content_Type = S.Content_Type,
          Updated_At = CURRENT_TIMESTAMP(), Updated_By = @updated_by
        WHEN NOT MATCHED THEN INSERT ({id_column}, Partnership, Content_Type, Updated_At, Updated_By)
          VALUES (S.{id_column}, S.Partnership, S.Content_Type, CURRENT_TIMESTAMP(), @updated_by)
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("updated_by", "STRING", updated_by)]
        ),
    ).result()


def get_platform_and_group_for_content_ids(client: bigquery.Client, content_ids: list) -> dict:
    """{Content_ID: {"Platform":..., "Group_ID": str or None}} for a list
    of raw content_ids -- used by the webapp's manual "Group Selected"
    feature to check platform-collision and existing-group state for
    whatever a human just checked in the Browse/Classify tables, in one
    query rather than one per item."""
    if not content_ids:
        return {}
    query = f"""
    SELECT ci.Content_ID, ci.Platform, m.Group_ID
    FROM `{_table_ref(CONTENT_ITEMS_TABLE)}` ci
    LEFT JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m ON ci.Content_ID = m.Content_ID
    WHERE ci.Content_ID IN UNNEST(@content_ids)
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("content_ids", "STRING", content_ids)]
        ),
    ).result()
    return {r["Content_ID"]: {"Platform": r["Platform"], "Group_ID": r["Group_ID"]} for r in rows}


def get_group_platforms(client: bigquery.Client, group_ids: list) -> dict:
    """{Group_ID: {Platform, ...}} for a list of group_ids -- the platform
    set of each group's current members, used to validate that a manual
    merge would never put two same-platform items in one group."""
    if not group_ids:
        return {}
    query = f"""
    SELECT m.Group_ID, ci.Platform
    FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
    WHERE m.Group_ID IN UNNEST(@group_ids)
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("group_ids", "STRING", group_ids)]
        ),
    ).result()
    result: dict = {}
    for r in rows:
        result.setdefault(r["Group_ID"], set()).add(r["Platform"])
    return result


def remove_member(client: bigquery.Client, group_id: str, content_id: str) -> None:
    """Splits one content_item back out of a group -- how a human corrects
    a wrong auto-match, e.g. after find_candidate_groups over-clusters."""
    query = f"""
    DELETE FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
    WHERE Group_ID = @group_id AND Content_ID = @content_id
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("group_id", "STRING", group_id),
                bigquery.ScalarQueryParameter("content_id", "STRING", content_id),
            ]
        ),
    ).result()


def get_group_classification(client: bigquery.Client, group_id: str):
    """(Partnership, Content_Type) for one group, or None if it doesn't
    exist. Used when accepting a pending match: if the group it's joining
    is already classified, that classification needs to be propagated to
    the newly-confirmed platform's own *_classifications table too."""
    rows = list(
        client.query(
            f"SELECT Partnership, Content_Type FROM `{_table_ref(CONTENT_GROUPS_TABLE)}` WHERE Group_ID = @group_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("group_id", "STRING", group_id)]
            ),
        ).result()
    )
    if not rows:
        return None
    return (rows[0]["Partnership"], rows[0]["Content_Type"])


def confirm_membership(client: bigquery.Client, group_id: str, content_id: str) -> None:
    """Accepts a pending auto-suggested match (Confirmed=False -> True)."""
    query = f"""
    UPDATE `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}`
    SET Confirmed = TRUE
    WHERE Group_ID = @group_id AND Content_ID = @content_id
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("group_id", "STRING", group_id),
                bigquery.ScalarQueryParameter("content_id", "STRING", content_id),
            ]
        ),
    ).result()


def set_classification(
    client: bigquery.Client, group_id: str, partnership: str, content_type: str, updated_by: str
) -> None:
    content_type = normalize_content_type(content_type)
    query = f"""
    UPDATE `{_table_ref(CONTENT_GROUPS_TABLE)}`
    SET Partnership = @partnership, Content_Type = @content_type,
        Updated_At = CURRENT_TIMESTAMP(), Updated_By = @updated_by
    WHERE Group_ID = @group_id
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("group_id", "STRING", group_id),
                bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
                bigquery.ScalarQueryParameter("content_type", "STRING", content_type),
                bigquery.ScalarQueryParameter("updated_by", "STRING", updated_by),
            ]
        ),
    ).result()


def list_partnerships(client: bigquery.Client) -> list:
    """Returns [{"Partnership": ..., "Content_Types": [...]}] for the
    dashboard's dropdowns, one row per partnership with its content types
    nested."""
    query = f"""
    SELECT p.Partnership, ARRAY_AGG(ct.Content_Type IGNORE NULLS ORDER BY ct.Content_Type) AS Content_Types
    FROM `{_table_ref(PARTNERSHIPS_TABLE)}` p
    LEFT JOIN `{_table_ref(PARTNERSHIP_CONTENT_TYPES_TABLE)}` ct ON p.Partnership = ct.Partnership
    GROUP BY p.Partnership
    ORDER BY p.Partnership
    """
    return [{"Partnership": r["Partnership"], "Content_Types": list(r["Content_Types"])} for r in client.query(query).result()]


def get_partnership_video_counts(client: bigquery.Client) -> dict:
    """{Partnership: video_count} for every partnership in the reference
    table, including 0 for one with no classified videos yet -- one
    content_groups row is one real-world video regardless of how many
    platforms it's cross-posted to or whether all its members are
    Confirmed, so this is a plain count of that table, not a join through
    content_group_members. Lets the Partnerships page show a count next
    to each partnership without a separate query per partnership."""
    query = f"""
    SELECT p.Partnership, COUNT(g.Group_ID) AS Video_Count
    FROM `{_table_ref(PARTNERSHIPS_TABLE)}` p
    LEFT JOIN `{_table_ref(CONTENT_GROUPS_TABLE)}` g ON p.Partnership = g.Partnership
    GROUP BY p.Partnership
    """
    return {r["Partnership"]: r["Video_Count"] for r in client.query(query).result()}


def delete_partnership(client: bigquery.Client, partnership: str) -> None:
    """Removes a partnership and its content types from the reference
    tables. Caller's responsibility to confirm it has no classified videos
    first (see get_partnership_video_counts) -- this doesn't touch
    content_groups at all, so a video still classified under a deleted
    partnership would keep that Partnership string, just no longer show up
    in the Partnerships list or its dropdowns."""
    for table in (PARTNERSHIP_CONTENT_TYPES_TABLE, PARTNERSHIPS_TABLE):
        client.query(
            f"DELETE FROM `{_table_ref(table)}` WHERE Partnership = @partnership",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("partnership", "STRING", partnership)]
            ),
        ).result()


def get_or_create_share_token(client: bigquery.Client, partnership: str) -> str:
    """Returns this partnership's share-link token, generating and
    persisting one on first request -- stable across calls, so a link
    once handed to a brand keeps working rather than rotating every time
    someone clicks "Get Share Link" again."""
    rows = list(
        client.query(
            f"SELECT Share_Token FROM `{_table_ref(PARTNERSHIPS_TABLE)}` WHERE Partnership = @partnership",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("partnership", "STRING", partnership)]
            ),
        ).result()
    )
    existing = rows[0]["Share_Token"] if rows else None
    if existing:
        return existing

    token = secrets.token_urlsafe(24)
    client.query(
        f"UPDATE `{_table_ref(PARTNERSHIPS_TABLE)}` SET Share_Token = @token WHERE Partnership = @partnership",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("token", "STRING", token),
                bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
            ]
        ),
    ).result()
    return token


def get_partnership_by_share_token(client: bigquery.Client, token: str):
    """The Partnership name for a share-link token, or None if it doesn't
    match anything -- used by the public /share/<token> route, which has
    no login, so an unrecognized token must fail closed (404) rather than
    leaking which tokens are valid."""
    rows = list(
        client.query(
            f"SELECT Partnership FROM `{_table_ref(PARTNERSHIPS_TABLE)}` WHERE Share_Token = @token",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("token", "STRING", token)]
            ),
        ).result()
    )
    return rows[0]["Partnership"] if rows else None


def add_partnership(client: bigquery.Client, partnership: str) -> None:
    # FROM (SELECT 1) is required -- BigQuery rejects a WHERE clause on a
    # SELECT with no FROM at all ("Query without FROM clause cannot have
    # a WHERE clause"), unlike some other SQL dialects.
    query = f"""
    INSERT INTO `{_table_ref(PARTNERSHIPS_TABLE)}` (Partnership, Created_At)
    SELECT @partnership, CURRENT_TIMESTAMP()
    FROM (SELECT 1)
    WHERE NOT EXISTS (
      SELECT 1 FROM `{_table_ref(PARTNERSHIPS_TABLE)}` WHERE Partnership = @partnership
    )
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("partnership", "STRING", partnership)]
        ),
    ).result()


def add_content_type(client: bigquery.Client, partnership: str, content_type: str) -> None:
    """Also ensures the parent partnership exists, so the dashboard can
    add a content type for a brand-new partnership in one action."""
    content_type = normalize_content_type(content_type)
    add_partnership(client, partnership)
    query = f"""
    INSERT INTO `{_table_ref(PARTNERSHIP_CONTENT_TYPES_TABLE)}` (Partnership, Content_Type, Created_At)
    SELECT @partnership, @content_type, CURRENT_TIMESTAMP()
    FROM (SELECT 1)
    WHERE NOT EXISTS (
      SELECT 1 FROM `{_table_ref(PARTNERSHIP_CONTENT_TYPES_TABLE)}`
      WHERE Partnership = @partnership AND Content_Type = @content_type
    )
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
                bigquery.ScalarQueryParameter("content_type", "STRING", content_type),
            ]
        ),
    ).result()


def get_partnership_groups(client: bigquery.Client, partnership: str) -> list:
    """Every confirmed content_group classified under one Partnership, each
    with its full member list (one row per platform it was posted to) and
    per-group summed stats -- the webapp's per-partnership report page.
    One group here is "one video posted" regardless of how many platforms
    it went out on, matching how a partner actually thinks about a
    campaign's deliverables."""
    query = f"""
    SELECT
      g.Group_ID, g.Content_Type,
      ARRAY_AGG(
        STRUCT(ci.Content_ID AS Content_ID, ci.Platform AS Platform, ci.Caption AS Caption,
               ci.Publish_Date AS Publish_Date, ci.Permalink AS Permalink,
               ci.Views AS Views, ci.Likes AS Likes, ci.Comments AS Comments, ci.Shares AS Shares)
        ORDER BY ci.Platform
      ) AS Members,
      MIN(ci.Publish_Date) AS Publish_Date,
      SUM(ci.Views) AS Views, SUM(ci.Likes) AS Likes,
      SUM(ci.Comments) AS Comments, SUM(ci.Shares) AS Shares,
      MAX(ci.Last_Synced_At) AS Last_Synced_At
    FROM `{_table_ref(CONTENT_GROUPS_TABLE)}` g
    JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m ON g.Group_ID = m.Group_ID AND m.Confirmed = TRUE
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
    WHERE g.Partnership = @partnership
    GROUP BY g.Group_ID, g.Content_Type
    ORDER BY Publish_Date DESC
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("partnership", "STRING", partnership)]
        ),
    ).result()
    return [dict(r) for r in rows]


def list_classification_queue(
    client: bigquery.Client, unclassified_only: bool = True, limit: int = 100
) -> list:
    """Returns one row per content_group, each with its member items
    nested -- the QuickBooks-style classification queue's main list.
    Ungrouped content_items (no group at all -- e.g. platform-exclusive
    content that never matched anything) are included too, as a
    single-item pseudo-group with Group_ID=None; the dashboard creates a
    real one-member group on first classification (see
    webapp/src/db.py classify()) rather than leaving single-platform
    content permanently unclassifiable."""
    query = f"""
    WITH grouped AS (
      SELECT
        g.Group_ID, g.Partnership, g.Content_Type,
        ARRAY_AGG(
          STRUCT(ci.Content_ID AS Content_ID, ci.Platform AS Platform, ci.Caption AS Caption, ci.Publish_Date AS Publish_Date,
                 ci.Permalink AS Permalink, ci.Views AS Views, ci.Platform_Post_ID AS Platform_Post_ID)
          ORDER BY ci.Platform
        ) AS Members,
        MAX(ci.Publish_Date) AS Latest_Date
      FROM `{_table_ref(CONTENT_GROUPS_TABLE)}` g
      JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m ON g.Group_ID = m.Group_ID AND m.Confirmed = TRUE
      JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
      GROUP BY g.Group_ID, g.Partnership, g.Content_Type
    ),
    ungrouped AS (
      SELECT
        CAST(NULL AS STRING) AS Group_ID, CAST(NULL AS STRING) AS Partnership, CAST(NULL AS STRING) AS Content_Type,
        [STRUCT(ci.Content_ID AS Content_ID, ci.Platform AS Platform, ci.Caption AS Caption, ci.Publish_Date AS Publish_Date,
                ci.Permalink AS Permalink, ci.Views AS Views, ci.Platform_Post_ID AS Platform_Post_ID)] AS Members,
        ci.Publish_Date AS Latest_Date
      FROM `{_table_ref(CONTENT_ITEMS_TABLE)}` ci
      LEFT JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m ON ci.Content_ID = m.Content_ID
      WHERE m.Content_ID IS NULL AND ci.API_Status = 'Active'
    )
    SELECT * FROM grouped
    UNION ALL
    SELECT * FROM ungrouped
    """
    rows = list(client.query(query).result())
    results = []
    for r in rows:
        partnership = r["Partnership"]
        if unclassified_only and partnership not in (None, "Unclassified"):
            continue
        results.append(
            {
                "Group_ID": r["Group_ID"],
                "Partnership": partnership,
                "Content_Type": r["Content_Type"],
                "Members": [dict(m) for m in r["Members"]],
                "Latest_Date": r["Latest_Date"],
            }
        )
    results.sort(key=lambda g: g["Latest_Date"] or "", reverse=True)
    return results[:limit]


def list_pending_matches(client: bigquery.Client, since=None) -> list:
    """Every unconfirmed (Confirmed=False) group membership, with the full
    group's members for context -- what the dashboard's review queue
    shows so a human can accept or reject each auto-suggested match.
    since: an optional datetime -- only pending candidates published on or
    after this are included, so very old backlog doesn't drown out recent,
    actionable ones."""
    since_clause = "AND ci.Publish_Date >= @since" if since is not None else ""
    query = f"""
    SELECT
      pending.Group_ID, pending.Content_ID AS Pending_Content_ID,
      pending.Match_Confidence,
      ci.Platform AS Pending_Platform, ci.Caption AS Pending_Caption,
      ci.Publish_Date AS Pending_Publish_Date, ci.Permalink AS Pending_Permalink,
      ARRAY_AGG(
        STRUCT(other_ci.Platform AS Platform, other_ci.Caption AS Caption,
               other_ci.Publish_Date AS Publish_Date, other_ci.Permalink AS Permalink)
        ORDER BY other_ci.Platform
      ) AS Existing_Members
    FROM `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` pending
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON pending.Content_ID = ci.Content_ID
    JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` other ON pending.Group_ID = other.Group_ID
      AND other.Content_ID != pending.Content_ID
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` other_ci ON other.Content_ID = other_ci.Content_ID
    WHERE pending.Confirmed = FALSE
    {since_clause}
    GROUP BY pending.Group_ID, pending.Content_ID, pending.Match_Confidence,
      ci.Platform, ci.Caption, ci.Publish_Date, ci.Permalink
    ORDER BY pending.Match_Confidence DESC
    """
    job_config = None
    if since is not None:
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("since", "TIMESTAMP", since)]
        )
    rows = list(client.query(query, job_config=job_config).result())
    return [
        {
            "Group_ID": r["Group_ID"],
            "Pending_Content_ID": r["Pending_Content_ID"],
            "Match_Confidence": r["Match_Confidence"],
            "Pending_Platform": r["Pending_Platform"],
            "Pending_Caption": r["Pending_Caption"],
            "Pending_Publish_Date": r["Pending_Publish_Date"],
            "Pending_Permalink": r["Pending_Permalink"],
            "Existing_Members": [dict(m) for m in r["Existing_Members"]],
        }
        for r in rows
    ]


def get_partner_report(client: bigquery.Client, partnership: str) -> list:
    """Every content_group for one partnership, with each platform's
    current metrics -- the query a partner-facing dashboard page runs."""
    query = f"""
    SELECT
      g.Group_ID, g.Partnership, g.Content_Type,
      ci.Platform, ci.Permalink, ci.Publish_Date,
      ci.Views, ci.Likes, ci.Comments, ci.Shares, ci.Saves,
      m.Confirmed
    FROM `{_table_ref(CONTENT_GROUPS_TABLE)}` g
    JOIN `{_table_ref(CONTENT_GROUP_MEMBERS_TABLE)}` m ON g.Group_ID = m.Group_ID
    JOIN `{_table_ref(CONTENT_ITEMS_TABLE)}` ci ON m.Content_ID = ci.Content_ID
    WHERE g.Partnership = @partnership
    ORDER BY ci.Publish_Date DESC
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("partnership", "STRING", partnership)]
        ),
    ).result()
    return [dict(r) for r in rows]


def record_account_stat(
    client: bigquery.Client, platform: str, account_username: str, account_id, followers
) -> None:
    """Upserts today's follower/subscriber snapshot for one account --
    MERGE keyed on (Platform, Account_Username, Snapshot_Date) so running
    a pipeline more than once in a day updates today's number in place
    instead of piling up duplicate rows, while every prior day's snapshot
    stays untouched -- that history is what makes a followers-over-time
    trend possible later, not just a single "latest" number. Called from
    each pipeline's own run(), right where it already has a freshly
    authenticated client and just fetched account/channel/page info."""
    now = datetime.now(timezone.utc)
    query = f"""
    MERGE `{_table_ref(ACCOUNT_STATS_TABLE)}` T
    USING (SELECT @platform AS Platform, @account_username AS Account_Username, @snapshot_date AS Snapshot_Date) S
    ON T.Platform = S.Platform AND T.Account_Username = S.Account_Username AND T.Snapshot_Date = S.Snapshot_Date
    WHEN MATCHED THEN UPDATE SET T.Followers = @followers, T.Account_ID = @account_id, T.Captured_At = @captured_at
    WHEN NOT MATCHED THEN
      INSERT (Platform, Account_Username, Account_ID, Followers, Snapshot_Date, Captured_At)
      VALUES (@platform, @account_username, @account_id, @followers, @snapshot_date, @captured_at)
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("platform", "STRING", platform),
                bigquery.ScalarQueryParameter("account_username", "STRING", account_username),
                bigquery.ScalarQueryParameter("account_id", "STRING", account_id),
                bigquery.ScalarQueryParameter("followers", "INT64", followers),
                bigquery.ScalarQueryParameter("snapshot_date", "DATE", now.date().isoformat()),
                bigquery.ScalarQueryParameter("captured_at", "TIMESTAMP", now.isoformat()),
            ]
        ),
    ).result()


def get_latest_account_stats(client: bigquery.Client) -> list:
    """The most recent Followers snapshot per (Platform, Account_Username)
    -- the media kit's headline follower/subscriber numbers."""
    query = f"""
    SELECT Platform, Account_Username, Account_ID, Followers, Snapshot_Date
    FROM `{_table_ref(ACCOUNT_STATS_TABLE)}`
    QUALIFY ROW_NUMBER() OVER (PARTITION BY Platform, Account_Username ORDER BY Snapshot_Date DESC) = 1
    ORDER BY Platform, Account_Username
    """
    return [dict(r) for r in client.query(query).result()]


def get_views_in_window(client: bigquery.Client, platform: str, account_username: str, days: int) -> int:
    """SUM(Views) across every content_item that account published in the
    last `days` days. Computed live from content_items rather than
    stored/snapshotted -- cheap to compute on demand (one indexed range
    scan) and never goes stale the way a cached number would."""
    query = f"""
    SELECT SUM(Views) AS Total_Views
    FROM `{_table_ref(CONTENT_ITEMS_TABLE)}`
    WHERE Platform = @platform AND Account_Username = @account_username
      AND Publish_Date >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
    """
    rows = list(
        client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("platform", "STRING", platform),
                    bigquery.ScalarQueryParameter("account_username", "STRING", account_username),
                    bigquery.ScalarQueryParameter("days", "INT64", days),
                ]
            ),
        ).result()
    )
    return (rows[0]["Total_Views"] or 0) if rows else 0


def list_stories(client: bigquery.Client, partnership: str = None) -> list:
    """Every manually-entered story, newest first. partnership: optional
    filter -- used by the partnership report page to fold story stats in
    alongside regular posts."""
    where = "WHERE Partnership = @partnership" if partnership else ""
    query = f"""
    SELECT Story_ID, Platform, Account_Username, Caption, Publish_Date,
      Views, Likes, Shares, Sticker_Taps, Replies, Tagged, Partnership, Content_Type, Updated_At
    FROM `{_table_ref(STORIES_TABLE)}`
    {where}
    ORDER BY Publish_Date DESC
    """
    job_config = None
    if partnership:
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("partnership", "STRING", partnership)]
        )
    return [dict(r) for r in client.query(query, job_config=job_config).result()]


def bulk_create_stories(client: bigquery.Client, rows: list) -> int:
    """rows: [{"Platform":..., "Account_Username":..., "Caption":...,
    "Publish_Date":..., "Views":..., "Likes":..., "Shares":...,
    "Sticker_Taps":..., "Replies":..., "Tagged":..., "Partnership":...,
    "Content_Type":...}] -- the Stories tab's "Submit All" button. One
    staging-table load plus one INSERT...SELECT for the whole batch, same
    reasoning as every other bulk write in this module. Returns how many
    rows were inserted."""
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    staging_rows = [
        {
            "Story_ID": str(uuid.uuid4()),
            "Platform": r.get("Platform"),
            "Account_Username": r.get("Account_Username"),
            "Caption": r.get("Caption"),
            "Publish_Date": r.get("Publish_Date"),
            "Views": r.get("Views"),
            "Likes": r.get("Likes"),
            "Shares": r.get("Shares"),
            "Sticker_Taps": r.get("Sticker_Taps"),
            "Replies": r.get("Replies"),
            "Tagged": r.get("Tagged"),
            "Partnership": r.get("Partnership") or "Unclassified",
            "Content_Type": r.get("Content_Type") or "Unclassified",
            "Created_At": now,
            "Updated_At": now,
        }
        for r in rows
    ]

    staging_id = _table_ref("stories_bulk_staging")
    client.create_table(bigquery.Table(staging_id, schema=STORIES_SCHEMA), exists_ok=True)
    client.load_table_from_json(
        staging_rows, staging_id,
        job_config=bigquery.LoadJobConfig(
            schema=STORIES_SCHEMA, write_disposition="WRITE_TRUNCATE",
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        ),
    ).result()
    columns = ", ".join(f.name for f in STORIES_SCHEMA)
    client.query(
        f"INSERT INTO `{_table_ref(STORIES_TABLE)}` ({columns}) SELECT {columns} FROM `{staging_id}`"
    ).result()
    return len(staging_rows)


_STORIES_EDITABLE_COLUMNS = {
    "Platform": "STRING", "Account_Username": "STRING", "Caption": "STRING",
    "Publish_Date": "DATE", "Views": "INT64", "Likes": "INT64", "Shares": "INT64",
    "Sticker_Taps": "INT64", "Replies": "INT64", "Tagged": "STRING",
    "Partnership": "STRING", "Content_Type": "STRING",
}


def update_story(client: bigquery.Client, story_id: str, fields: dict) -> None:
    """fields: any subset of _STORIES_EDITABLE_COLUMNS' keys -- the
    Stories tab's per-row Save when editing an existing story."""
    set_clauses = [f"{col} = @{col}" for col in fields if col in _STORIES_EDITABLE_COLUMNS]
    if not set_clauses:
        return
    params = [bigquery.ScalarQueryParameter("story_id", "STRING", story_id)]
    for col, value in fields.items():
        if col not in _STORIES_EDITABLE_COLUMNS:
            continue
        params.append(bigquery.ScalarQueryParameter(col, _STORIES_EDITABLE_COLUMNS[col], value))
    query = f"""
    UPDATE `{_table_ref(STORIES_TABLE)}`
    SET {", ".join(set_clauses)}, Updated_At = CURRENT_TIMESTAMP()
    WHERE Story_ID = @story_id
    """
    client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()


def delete_story(client: bigquery.Client, story_id: str) -> None:
    client.query(
        f"DELETE FROM `{_table_ref(STORIES_TABLE)}` WHERE Story_ID = @story_id",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("story_id", "STRING", story_id)]
        ),
    ).result()

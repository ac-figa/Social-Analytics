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
import uuid
from datetime import datetime, timezone

from google.cloud import bigquery

from . import config

log = logging.getLogger(__name__)

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
]

PARTNERSHIP_CONTENT_TYPES_SCHEMA = [
    bigquery.SchemaField("Partnership", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Content_Type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Created_At", "TIMESTAMP"),
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
    if not content_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "Group_ID": group_id,
            "Content_ID": cid,
            "Match_Method": match_method,
            "Match_Confidence": match_confidence,
            "Confirmed": confirmed,
            "Added_At": now,
        }
        for cid in content_ids
    ]
    errors = client.insert_rows_json(_table_ref(CONTENT_GROUP_MEMBERS_TABLE), rows)
    if errors:
        raise RuntimeError(f"Failed to add group members: {errors}")


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
          STRUCT(ci.Platform AS Platform, ci.Caption AS Caption, ci.Publish_Date AS Publish_Date,
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
        [STRUCT(ci.Platform AS Platform, ci.Caption AS Caption, ci.Publish_Date AS Publish_Date,
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

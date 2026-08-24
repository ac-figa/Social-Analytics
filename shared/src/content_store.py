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
    ):
        table_id = _table_ref(name)
        try:
            client.get_table(table_id)
        except Exception:
            log.info("Creating table %s", table_id)
            client.create_table(bigquery.Table(table_id, schema=schema))


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

    update_clause = ", ".join(f"T.{c} = S.{c}" for c in _CONTENT_ITEMS_UPDATE_COLUMNS)
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

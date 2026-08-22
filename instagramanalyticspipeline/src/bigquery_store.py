"""
BigQuery persistence layer.

Upsert strategy: truncate-and-load a staging table each run, then MERGE
into the permanent table. This is the standard, debuggable BigQuery
upsert pattern -- the staging table's contents can always be inspected if
a run looks wrong, and the MERGE itself is a single atomic statement.

Classification preservation: Partnership/Content_Type are attached to
each row by the pipeline (from instagram_classifications) *before* it
reaches this module, so the MERGE's UPDATE SET simply reassigns the same
value it read -- idempotent, and correct for both existing and brand-new
posts. Data/Data_Comment are user-owned free-text fields with no
classifications-table equivalent, so they are deliberately excluded from
UPDATE SET and only ever set on first INSERT.
"""
import logging

from google.cloud import bigquery

from . import config

log = logging.getLogger(__name__)

MASTER_TABLE = "instagram_master"
STAGING_TABLE = "instagram_master_staging"
CLASSIFICATIONS_TABLE = "instagram_classifications"
HISTORY_TABLE = "instagram_insights_history"
HISTORY_STAGING_TABLE = "instagram_insights_history_staging"

MASTER_SCHEMA = [
    bigquery.SchemaField("Post_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Account_ID", "STRING"),
    bigquery.SchemaField("Account_Username", "STRING"),
    bigquery.SchemaField("Account_Name", "STRING"),
    bigquery.SchemaField("Description", "STRING"),
    bigquery.SchemaField("Duration", "FLOAT64"),
    bigquery.SchemaField("Publish_Date", "TIMESTAMP"),
    bigquery.SchemaField("Permalink", "STRING"),
    bigquery.SchemaField("Post_Type", "STRING"),
    bigquery.SchemaField("Data_Comment", "STRING"),
    bigquery.SchemaField("Data", "STRING"),
    bigquery.SchemaField("Views", "INT64"),
    bigquery.SchemaField("Views_Organic", "INT64"),
    bigquery.SchemaField("Reach", "INT64"),
    bigquery.SchemaField("Likes", "INT64"),
    bigquery.SchemaField("Shares", "INT64"),
    bigquery.SchemaField("Follows", "INT64"),
    bigquery.SchemaField("Comments", "INT64"),
    bigquery.SchemaField("Saves", "INT64"),
    bigquery.SchemaField("Total_Interactions", "INT64"),
    bigquery.SchemaField("Watch_Time", "FLOAT64"),
    bigquery.SchemaField("Average_Watch_Time", "FLOAT64"),
    bigquery.SchemaField("Tagged", "STRING"),
    bigquery.SchemaField("Collabed", "BOOL"),
    bigquery.SchemaField("Collaborator_Usernames", "STRING"),
    bigquery.SchemaField("Media_Audio_Type", "STRING"),
    bigquery.SchemaField("Is_Shared_To_Feed", "BOOL"),
    bigquery.SchemaField("Partnership", "STRING"),
    bigquery.SchemaField("Content_Type", "STRING"),
    bigquery.SchemaField("Suggested_Partnership", "STRING"),
    bigquery.SchemaField("API_Status", "STRING"),
    bigquery.SchemaField("Last_Synced_At", "TIMESTAMP"),
]

CLASSIFICATIONS_SCHEMA = [
    bigquery.SchemaField("Post_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Partnership", "STRING"),
    bigquery.SchemaField("Content_Type", "STRING"),
    bigquery.SchemaField("Updated_At", "TIMESTAMP"),
    bigquery.SchemaField("Updated_By", "STRING"),
]

HISTORY_SCHEMA = [
    bigquery.SchemaField("Snapshot_Date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("Post_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Views", "INT64"),
    bigquery.SchemaField("Views_Organic", "INT64"),
    bigquery.SchemaField("Reach", "INT64"),
    bigquery.SchemaField("Likes", "INT64"),
    bigquery.SchemaField("Comments", "INT64"),
    bigquery.SchemaField("Shares", "INT64"),
    bigquery.SchemaField("Saves", "INT64"),
    bigquery.SchemaField("Watch_Time", "FLOAT64"),
    bigquery.SchemaField("Total_Interactions", "INT64"),
]

# Columns updated on an existing Instagram_Master row. Data/Data_Comment
# are deliberately absent -- see module docstring.
_MASTER_UPDATE_COLUMNS = [f.name for f in MASTER_SCHEMA if f.name != "Post_ID"]
_MASTER_UPDATE_COLUMNS = [c for c in _MASTER_UPDATE_COLUMNS if c not in ("Data", "Data_Comment")]


def get_client() -> bigquery.Client:
    return bigquery.Client(project=config.BQ_PROJECT_ID)


def _table_ref(name: str) -> str:
    return f"{config.BQ_PROJECT_ID}.{config.BQ_DATASET}.{name}"


def ensure_schema(client: bigquery.Client) -> None:
    dataset_ref = bigquery.DatasetReference(config.BQ_PROJECT_ID, config.BQ_DATASET)
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        log.info("Creating dataset %s", config.BQ_DATASET)
        client.create_dataset(bigquery.Dataset(dataset_ref))

    for name, schema in (
        (MASTER_TABLE, MASTER_SCHEMA),
        (CLASSIFICATIONS_TABLE, CLASSIFICATIONS_SCHEMA),
        (HISTORY_TABLE, HISTORY_SCHEMA),
    ):
        table_id = _table_ref(name)
        try:
            client.get_table(table_id)
        except Exception:
            log.info("Creating table %s", table_id)
            client.create_table(bigquery.Table(table_id, schema=schema))


def load_classifications(client: bigquery.Client) -> dict:
    """Returns {Post_ID: {"Partnership": ..., "Content_Type": ...}}."""
    query = f"SELECT Post_ID, Partnership, Content_Type FROM `{_table_ref(CLASSIFICATIONS_TABLE)}`"
    try:
        rows = list(client.query(query).result())
    except Exception as e:
        log.warning("Could not read existing classifications (first run?): %s", e)
        return {}
    return {
        r["Post_ID"]: {"Partnership": r["Partnership"], "Content_Type": r["Content_Type"]}
        for r in rows
    }


def upsert_classification(
    client: bigquery.Client, post_id: str, partnership: str, content_type: str, updated_by: str
) -> None:
    """Used by classify.py. Also a plain MERGE keyed on Post_ID."""
    query = f"""
    MERGE `{_table_ref(CLASSIFICATIONS_TABLE)}` T
    USING (SELECT @post_id AS Post_ID) S
    ON T.Post_ID = S.Post_ID
    WHEN MATCHED THEN UPDATE SET
      Partnership = @partnership, Content_Type = @content_type,
      Updated_At = CURRENT_TIMESTAMP(), Updated_By = @updated_by
    WHEN NOT MATCHED THEN INSERT (Post_ID, Partnership, Content_Type, Updated_At, Updated_By)
      VALUES (@post_id, @partnership, @content_type, CURRENT_TIMESTAMP(), @updated_by)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("post_id", "STRING", post_id),
            bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
            bigquery.ScalarQueryParameter("content_type", "STRING", content_type),
            bigquery.ScalarQueryParameter("updated_by", "STRING", updated_by),
        ]
    )
    client.query(query, job_config=job_config).result()


def upsert_master_rows(client: bigquery.Client, rows: list) -> None:
    if not rows:
        log.info("No master rows to upsert.")
        return

    staging_id = _table_ref(STAGING_TABLE)
    client.create_table(bigquery.Table(staging_id, schema=MASTER_SCHEMA), exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        schema=MASTER_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(rows, staging_id, job_config=job_config).result()

    update_clause = ", ".join(f"T.{c} = S.{c}" for c in _MASTER_UPDATE_COLUMNS)
    insert_columns = [f.name for f in MASTER_SCHEMA]
    insert_values = ", ".join(f"S.{c}" for c in insert_columns)

    merge_sql = f"""
    MERGE `{_table_ref(MASTER_TABLE)}` T
    USING `{staging_id}` S
    ON T.Post_ID = S.Post_ID
    WHEN MATCHED THEN UPDATE SET {update_clause}
    WHEN NOT MATCHED THEN INSERT ({", ".join(insert_columns)})
      VALUES ({insert_values})
    """
    client.query(merge_sql).result()
    log.info("Upserted %d rows into %s", len(rows), MASTER_TABLE)


def mark_missing_as_deleted(client: bigquery.Client, current_post_ids: list) -> None:
    """Anything Active in the master table but absent from this run's pull
    is marked, never deleted, so history is preserved."""
    query = f"""
    UPDATE `{_table_ref(MASTER_TABLE)}`
    SET API_Status = 'Deleted_or_Unavailable'
    WHERE API_Status = 'Active' AND Post_ID NOT IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", current_post_ids)]
    )
    result = client.query(query, job_config=job_config).result()
    if result.num_dml_affected_rows:
        log.info("Marked %d posts as Deleted_or_Unavailable", result.num_dml_affected_rows)


def insert_history_snapshot(client: bigquery.Client, rows: list, snapshot_date: str) -> None:
    if not rows:
        return

    for r in rows:
        r["Snapshot_Date"] = snapshot_date

    staging_id = _table_ref(HISTORY_STAGING_TABLE)
    client.create_table(bigquery.Table(staging_id, schema=HISTORY_SCHEMA), exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        schema=HISTORY_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(rows, staging_id, job_config=job_config).result()

    merge_sql = f"""
    MERGE `{_table_ref(HISTORY_TABLE)}` T
    USING `{staging_id}` S
    ON T.Post_ID = S.Post_ID AND T.Snapshot_Date = S.Snapshot_Date
    WHEN NOT MATCHED THEN INSERT (Snapshot_Date, Post_ID, Views, Views_Organic, Reach, Likes, Comments, Shares, Saves, Watch_Time, Total_Interactions)
      VALUES (S.Snapshot_Date, S.Post_ID, S.Views, S.Views_Organic, S.Reach, S.Likes, S.Comments, S.Shares, S.Saves, S.Watch_Time, S.Total_Interactions)
    """
    client.query(merge_sql).result()
    log.info("Inserted %d history snapshot rows for %s (deduped)", len(rows), snapshot_date)

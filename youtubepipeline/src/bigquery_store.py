"""
BigQuery persistence layer. Same staging-table + MERGE upsert pattern as
instagramanalyticspipeline/src/bigquery_store.py -- see that module's
docstring for the reasoning.
"""
import logging

from google.cloud import bigquery

from . import config

log = logging.getLogger(__name__)

MASTER_TABLE = "youtube_master"
STAGING_TABLE = "youtube_master_staging"
CLASSIFICATIONS_TABLE = "youtube_classifications"
HISTORY_TABLE = "youtube_insights_history"
HISTORY_STAGING_TABLE = "youtube_insights_history_staging"

MASTER_SCHEMA = [
    bigquery.SchemaField("Video_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Channel_ID", "STRING"),
    bigquery.SchemaField("Channel_Title", "STRING"),
    bigquery.SchemaField("Title", "STRING"),
    bigquery.SchemaField("Description", "STRING"),
    bigquery.SchemaField("Duration", "FLOAT64"),
    bigquery.SchemaField("Publish_Date", "TIMESTAMP"),
    bigquery.SchemaField("Permalink", "STRING"),
    bigquery.SchemaField("Post_Type", "STRING"),
    bigquery.SchemaField("Views", "INT64"),
    bigquery.SchemaField("Likes", "INT64"),
    bigquery.SchemaField("Comments", "INT64"),
    bigquery.SchemaField("Shares", "INT64"),
    bigquery.SchemaField("Partnership", "STRING"),
    bigquery.SchemaField("Content_Type", "STRING"),
    bigquery.SchemaField("Suggested_Partnership", "STRING"),
    bigquery.SchemaField("API_Status", "STRING"),
    bigquery.SchemaField("Last_Synced_At", "TIMESTAMP"),
]

CLASSIFICATIONS_SCHEMA = [
    bigquery.SchemaField("Video_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Partnership", "STRING"),
    bigquery.SchemaField("Content_Type", "STRING"),
    bigquery.SchemaField("Updated_At", "TIMESTAMP"),
    bigquery.SchemaField("Updated_By", "STRING"),
]

HISTORY_SCHEMA = [
    bigquery.SchemaField("Snapshot_Date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("Video_ID", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("Views", "INT64"),
    bigquery.SchemaField("Likes", "INT64"),
    bigquery.SchemaField("Comments", "INT64"),
]

_MASTER_UPDATE_COLUMNS = [f.name for f in MASTER_SCHEMA if f.name != "Video_ID"]


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
    query = f"SELECT Video_ID, Partnership, Content_Type FROM `{_table_ref(CLASSIFICATIONS_TABLE)}`"
    try:
        rows = list(client.query(query).result())
    except Exception as e:
        log.warning("Could not read existing classifications (first run?): %s", e)
        return {}
    return {
        r["Video_ID"]: {"Partnership": r["Partnership"], "Content_Type": r["Content_Type"]}
        for r in rows
    }


def upsert_classification(
    client: bigquery.Client, video_id: str, partnership: str, content_type: str, updated_by: str
) -> None:
    query = f"""
    MERGE `{_table_ref(CLASSIFICATIONS_TABLE)}` T
    USING (SELECT @video_id AS Video_ID) S
    ON T.Video_ID = S.Video_ID
    WHEN MATCHED THEN UPDATE SET
      Partnership = @partnership, Content_Type = @content_type,
      Updated_At = CURRENT_TIMESTAMP(), Updated_By = @updated_by
    WHEN NOT MATCHED THEN INSERT (Video_ID, Partnership, Content_Type, Updated_At, Updated_By)
      VALUES (@video_id, @partnership, @content_type, CURRENT_TIMESTAMP(), @updated_by)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("video_id", "STRING", video_id),
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
    ON T.Video_ID = S.Video_ID
    WHEN MATCHED THEN UPDATE SET {update_clause}
    WHEN NOT MATCHED THEN INSERT ({", ".join(insert_columns)})
      VALUES ({insert_values})
    """
    client.query(merge_sql).result()
    log.info("Upserted %d rows into %s", len(rows), MASTER_TABLE)


def mark_missing_as_deleted(client: bigquery.Client, current_video_ids: list) -> None:
    query = f"""
    UPDATE `{_table_ref(MASTER_TABLE)}`
    SET API_Status = 'Deleted_or_Unavailable'
    WHERE API_Status = 'Active' AND Video_ID NOT IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", current_video_ids)]
    )
    result = client.query(query, job_config=job_config).result()
    if result.num_dml_affected_rows:
        log.info("Marked %d videos as Deleted_or_Unavailable", result.num_dml_affected_rows)


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
    ON T.Video_ID = S.Video_ID AND T.Snapshot_Date = S.Snapshot_Date
    WHEN NOT MATCHED THEN INSERT (Snapshot_Date, Video_ID, Views, Likes, Comments)
      VALUES (S.Snapshot_Date, S.Video_ID, S.Views, S.Likes, S.Comments)
    """
    client.query(merge_sql).result()
    log.info("Inserted %d history snapshot rows for %s (deduped)", len(rows), snapshot_date)

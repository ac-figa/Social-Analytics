-- Reference DDL. The pipeline creates/manages these tables itself
-- (src/bigquery_store.py) on first run -- this file is for humans who want
-- to review the schema or create the tables manually ahead of time.
--
-- Replace `${PROJECT}.${DATASET}` before running by hand.

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.tiktok_master` (
  Video_ID                STRING NOT NULL,   -- TikTok video ID, primary key
  Title                    STRING,            -- caption
  Duration                 FLOAT64,           -- seconds; TikTok exposes this reliably
  Publish_Date             TIMESTAMP,
  Permalink                STRING,
  Views                    INT64,
  Likes                    INT64,
  Comments                 INT64,
  Shares                   INT64,             -- TikTok exposes this directly, unlike Facebook/Instagram
  Partnership              STRING,            -- manual, sourced from tiktok_classifications
  Content_Type             STRING,            -- manual, sourced from tiktok_classifications
  Suggested_Partnership    STRING,            -- heuristic hint only, never authoritative
  API_Status               STRING,            -- Active / Deleted_or_Unavailable
  Last_Synced_At           TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.tiktok_classifications` (
  Video_ID      STRING NOT NULL,  -- primary key, matches tiktok_master.Video_ID
  Partnership   STRING,
  Content_Type  STRING,
  Updated_At    TIMESTAMP,
  Updated_By    STRING
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.tiktok_insights_history` (
  Snapshot_Date   DATE NOT NULL,
  Video_ID        STRING NOT NULL,
  Views           INT64,
  Likes           INT64,
  Comments        INT64,
  Shares          INT64
  -- Unique per (Snapshot_Date, Video_ID); dedup enforced by the pipeline's MERGE.
);

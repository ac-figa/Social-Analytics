-- Reference DDL. The pipeline creates/manages these tables itself
-- (src/bigquery_store.py) on first run -- this file is for humans who want
-- to review the schema or create the tables manually ahead of time.
--
-- Replace `${PROJECT}.${DATASET}` before running by hand.

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.youtube_master` (
  Video_ID                STRING NOT NULL,   -- YouTube video ID, primary key
  Channel_ID               STRING,
  Channel_Title             STRING,
  Title                     STRING,
  Description               STRING,
  Duration                  FLOAT64,           -- seconds; YouTube exposes this reliably, unlike Instagram
  Publish_Date              TIMESTAMP,
  Permalink                 STRING,
  Post_Type                 STRING,            -- Short / Video (best-effort heuristic, see docs/SETUP.md)
  Views                     INT64,
  Likes                     INT64,             -- NULL if the creator has hidden their like count
  Comments                  INT64,
  Shares                    INT64,             -- always NULL: not exposed by the YouTube Data API
  Partnership               STRING,            -- manual, sourced from youtube_classifications
  Content_Type              STRING,            -- manual, sourced from youtube_classifications
  Suggested_Partnership     STRING,            -- heuristic hint only, never authoritative
  API_Status                STRING,            -- Active / Deleted_or_Unavailable
  Last_Synced_At            TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.youtube_classifications` (
  Video_ID      STRING NOT NULL,  -- primary key, matches youtube_master.Video_ID
  Partnership   STRING,
  Content_Type  STRING,
  Updated_At    TIMESTAMP,
  Updated_By    STRING
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.youtube_insights_history` (
  Snapshot_Date   DATE NOT NULL,
  Video_ID        STRING NOT NULL,
  Views           INT64,
  Likes           INT64,
  Comments        INT64
  -- Unique per (Snapshot_Date, Video_ID); dedup enforced by the pipeline's MERGE.
);

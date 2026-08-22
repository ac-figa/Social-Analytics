-- Reference DDL. The pipeline creates/manages these tables itself
-- (src/bigquery_store.py) on first run -- this file is for humans who want
-- to review the schema or create the tables manually ahead of time.
--
-- Replace `${PROJECT}.${DATASET}` before running by hand.

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.facebook_master` (
  Video_ID                STRING NOT NULL,   -- Facebook video ID, primary key
  Page_ID                 STRING,
  Page_Name                STRING,
  Description              STRING,            -- caption
  Length                   FLOAT64,           -- seconds; Facebook does expose this, unlike Instagram
  Publish_Date             TIMESTAMP,
  Permalink                STRING,
  Views                    INT64,             -- from Page Post Insights (post_video_views), see docs/SETUP.md
  Views_Organic             INT64,             -- from Page Post Insights (post_video_views_organic)
  Impressions               INT64,             -- always NULL: no working metric, see docs/SETUP.md
  Average_Watch_Time        FLOAT64,           -- from Page Post Insights (post_video_avg_time_watched), ms
  Watch_Time                FLOAT64,           -- from Page Post Insights (post_video_view_time), ms
  Likes                     INT64,
  Comments                  INT64,
  Shares                    INT64,             -- always NULL: not returned by the API, see docs/SETUP.md
  Partnership               STRING,            -- manual, sourced from facebook_classifications
  Content_Type              STRING,            -- manual, sourced from facebook_classifications
  Suggested_Partnership     STRING,            -- heuristic hint only, never authoritative
  API_Status                STRING,            -- Active / Deleted_or_Unavailable
  Last_Synced_At            TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.facebook_classifications` (
  Video_ID      STRING NOT NULL,  -- primary key, matches facebook_master.Video_ID
  Partnership   STRING,
  Content_Type  STRING,
  Updated_At    TIMESTAMP,
  Updated_By    STRING
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.facebook_insights_history` (
  Snapshot_Date   DATE NOT NULL,
  Video_ID        STRING NOT NULL,
  Views           INT64,
  Views_Organic   INT64,
  Impressions     INT64,
  Likes           INT64,
  Comments        INT64,
  Watch_Time      FLOAT64
  -- Unique per (Snapshot_Date, Video_ID); dedup enforced by the pipeline's MERGE.
);

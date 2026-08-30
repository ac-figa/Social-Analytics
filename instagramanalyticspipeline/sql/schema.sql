-- Reference DDL. The pipeline creates/manages these tables itself
-- (src/bigquery_store.py) on first run -- this file is for humans who want
-- to review the schema or create the tables manually ahead of time.
--
-- Replace `${PROJECT}.${DATASET}` before running by hand.

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.instagram_master` (
  Post_ID                 STRING NOT NULL,   -- Instagram media ID, primary key
  Account_ID              STRING,
  Account_Username        STRING,
  Account_Name            STRING,
  Description             STRING,            -- caption
  Duration                FLOAT64,           -- always NULL: not exposed by the Graph API
  Publish_Date            TIMESTAMP,
  Permalink               STRING,
  Post_Type               STRING,            -- Reel / Video / Carousel / Image
  Data_Comment            STRING,            -- free-text, user-owned, never touched by refresh
  Data                    STRING,            -- free-text, user-owned, never touched by refresh
  Views                   INT64,
  Reach                   INT64,
  Likes                   INT64,
  Shares                  INT64,
  Follows                 INT64,
  Comments                INT64,
  Saves                   INT64,
  Total_Interactions      INT64,
  Watch_Time              FLOAT64,
  Average_Watch_Time      FLOAT64,
  Tagged                  STRING,            -- not reliably available via API; see Suggested_Partnership
  Collabed                BOOL,
  Collaborator_Usernames  STRING,
  Media_Audio_Type        STRING,
  Is_Shared_To_Feed       BOOL,
  Partnership             STRING,            -- manual, sourced from instagram_classifications
  Content_Type            STRING,            -- manual, sourced from instagram_classifications
  Suggested_Partnership   STRING,            -- heuristic hint only, never authoritative
  API_Status               STRING,            -- Active / Deleted_or_Unavailable
  Last_Synced_At           TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.instagram_classifications` (
  Post_ID       STRING NOT NULL,  -- primary key, matches instagram_master.Post_ID
  Partnership   STRING,
  Content_Type  STRING,
  Updated_At    TIMESTAMP,
  Updated_By    STRING
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.instagram_insights_history` (
  Snapshot_Date       DATE NOT NULL,
  Post_ID             STRING NOT NULL,
  Views               INT64,
  Reach               INT64,
  Likes                INT64,
  Comments             INT64,
  Shares               INT64,
  Saves                INT64,
  Watch_Time           FLOAT64,
  Total_Interactions   INT64
  -- Unique per (Snapshot_Date, Post_ID); dedup enforced by the pipeline's MERGE.
);

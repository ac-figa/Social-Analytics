-- Reference DDL for the cross-platform content layer. The pipeline
-- creates/manages these tables itself (shared/src/content_store.py) on
-- first run -- this file is for humans who want to review the schema or
-- create the tables manually ahead of time.
--
-- Replace `${PROJECT}.${DATASET}` before running by hand. ${DATASET}
-- defaults to `social_analytics` (see SHARED_BQ_DATASET in .env) and is
-- separate from each platform's own dataset (e.g. instagram_analytics) --
-- this layer is additive, it never replaces a platform's detailed table.

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.content_items` (
  Content_ID        STRING NOT NULL,  -- "{platform}:{platform_post_id}", primary key
  Platform          STRING NOT NULL,  -- Instagram / YouTube / TikTok / Facebook
  Platform_Post_ID  STRING NOT NULL,
  Account_Username  STRING,
  Caption           STRING,
  Publish_Date      TIMESTAMP,
  Permalink         STRING,
  Post_Type         STRING,
  Thumbnail_URL     STRING,
  Views             INT64,
  Likes             INT64,
  Comments          INT64,
  Shares            INT64,
  Saves             INT64,
  API_Status        STRING,           -- Active / Deleted_or_Unavailable
  Last_Synced_At    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.content_groups` (
  Group_ID      STRING NOT NULL,  -- UUID, primary key
  Partnership   STRING,
  Content_Type  STRING,
  Notes         STRING,
  Created_At    TIMESTAMP,
  Updated_At    TIMESTAMP,
  Updated_By    STRING
);

CREATE TABLE IF NOT EXISTS `${PROJECT}.${DATASET}.content_group_members` (
  Group_ID          STRING NOT NULL,  -- FK -> content_groups.Group_ID
  Content_ID        STRING NOT NULL,  -- FK -> content_items.Content_ID
  Match_Method      STRING,           -- auto / manual
  Match_Confidence  FLOAT64,          -- NULL for manual links
  Confirmed         BOOL,             -- FALSE = auto-suggested, awaiting human review
  Added_At          TIMESTAMP
  -- A Content_ID should appear at most once across all groups -- enforced
  -- by application logic (get_ungrouped_items), not a DB constraint.
);

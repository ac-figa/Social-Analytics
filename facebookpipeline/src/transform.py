"""
Normalizes raw Graph API responses into one Facebook_Master row per video.

Same null vs. 0 convention as the Instagram pipeline (see its
transform.py docstring): 0 means the API explicitly returned zero, None
means the metric wasn't returned at all -- never silently coerced to 0.
"""
from datetime import datetime, timezone

FB_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"  # e.g. "2024-01-01T12:00:00+0000"


def _normalize_timestamp(raw: str):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, FB_TIMESTAMP_FORMAT).isoformat()
    except ValueError:
        return raw


def parse_timestamp(raw: str):
    """See instagramanalyticspipeline/src/transform.py's twin of this
    function -- returns a timezone-aware datetime, or None if missing/
    unparseable (callers should treat None as "refresh it anyway")."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, FB_TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _normalize_permalink(raw: str):
    """Unlike Instagram's `permalink` field, a video node's `permalink_url`
    from the Graph API comes back as a path relative to facebook.com (e.g.
    "/PageName/videos/1234567890/"), not a full URL -- confirmed live,
    Aug 2026: every dashboard link built directly from this field resolved
    against the *dashboard's own* origin instead of Facebook's, landing on
    a broken/unrelated page. Prefix it with the real domain whenever it
    isn't absolute already."""
    if not raw:
        return None
    if raw.startswith("/"):
        return f"https://www.facebook.com{raw}"
    return raw


def build_master_row(video_detail: dict, insights: dict, page_info: dict) -> dict:
    """video_detail: result of graph_client.get_video_details()[video_id]
    insights: result of graph_client.get_post_insights()[video_id] --
    Page Post Insights, NOT /video_insights (broken for this account,
    see graph_client.py's module docstring)
    page_info: {"id":..., "name":..., "username":...} fetched once

    Views prefers the insights value (post_video_views -- a well-defined
    "viewed for 3+ seconds" count, consistent with how views are counted
    elsewhere in this project) and falls back to the video object's own
    `views` field only if insights are missing entirely (e.g. no post_id).
    Impressions and Shares stay None -- confirmed unavailable, not just
    unfetched, see graph_client.py's module docstring.
    """
    insights = insights or {}
    views = insights.get("post_video_views")
    if views is None:
        views = video_detail.get("views")

    return {
        "Video_ID": video_detail["id"],
        "Page_ID": page_info.get("id"),
        "Page_Name": page_info.get("name"),
        "Description": video_detail.get("description"),
        "Length": video_detail.get("length"),
        "Publish_Date": _normalize_timestamp(video_detail.get("created_time")),
        "Permalink": _normalize_permalink(video_detail.get("permalink_url")),
        "Views": views,
        "Views_Organic": insights.get("post_video_views_organic"),
        "Impressions": None,  # confirmed unavailable -- see graph_client.py module docstring
        "Average_Watch_Time": insights.get("post_video_avg_time_watched"),
        "Watch_Time": insights.get("post_video_view_time"),
        "Likes": (video_detail.get("likes") or {}).get("summary", {}).get("total_count"),
        "Comments": (video_detail.get("comments") or {}).get("summary", {}).get("total_count"),
        "Shares": None,  # confirmed unavailable -- see graph_client.py module docstring
        "Partnership": None,  # filled by pipeline.py from facebook_classifications
        "Content_Type": None,
        "Suggested_Partnership": None,
        "API_Status": "Active",
        "Last_Synced_At": datetime.now(timezone.utc).isoformat(),
    }


def build_history_row(master_row: dict, snapshot_date: str) -> dict:
    return {
        "Snapshot_Date": snapshot_date,
        "Video_ID": master_row["Video_ID"],
        "Views": master_row.get("Views"),
        "Views_Organic": master_row.get("Views_Organic"),
        "Impressions": master_row.get("Impressions"),
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Watch_Time": master_row.get("Watch_Time"),
    }


def to_content_item(master_row: dict) -> dict:
    """Normalizes a Facebook_Master row into the shared cross-platform
    content_items shape (see shared/src/content_store.py)."""
    return {
        "Content_ID": f"facebook:{master_row['Video_ID']}",
        "Platform": "Facebook",
        "Platform_Post_ID": master_row["Video_ID"],
        "Account_Username": master_row.get("Page_Name"),
        "Caption": master_row.get("Description"),
        "Publish_Date": master_row.get("Publish_Date"),
        "Permalink": master_row.get("Permalink"),
        "Post_Type": "Video",
        "Thumbnail_URL": None,
        "Duration": master_row.get("Length"),
        "Views": master_row.get("Views"),
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Shares": master_row.get("Shares"),
        "Saves": None,  # not exposed for Facebook video
        "API_Status": master_row.get("API_Status"),
        "Last_Synced_At": master_row.get("Last_Synced_At"),
    }

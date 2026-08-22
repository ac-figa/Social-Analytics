"""
Normalizes raw TikTok API v2 video objects into one TikTok_Master row per
video.

Same null vs. 0 convention as the other pipelines: 0 means the API
explicitly returned zero, None means the field wasn't returned at all.
"""
from datetime import datetime, timezone

from . import config


def build_master_row(video: dict) -> dict:
    """video: one item from TikTokClient.get_all_videos() -- already
    contains full stats, no separate details/insights call needed (see
    tiktok_client.py's module docstring)."""
    create_time = video.get("create_time")
    publish_date = (
        datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
        if create_time is not None
        else None
    )

    return {
        "Video_ID": video["id"],
        "Title": video.get("title"),
        "Duration": video.get("duration"),
        "Publish_Date": publish_date,
        "Permalink": f"https://www.tiktok.com/@{config.TIKTOK_USERNAME}/video/{video['id']}",
        "Views": video.get("view_count"),
        "Likes": video.get("like_count"),
        "Comments": video.get("comment_count"),
        "Shares": video.get("share_count"),
        "Partnership": None,  # filled by pipeline.py from tiktok_classifications
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
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Shares": master_row.get("Shares"),
    }


def to_content_item(master_row: dict) -> dict:
    """Normalizes a TikTok_Master row into the shared cross-platform
    content_items shape (see shared/src/content_store.py)."""
    return {
        "Content_ID": f"tiktok:{master_row['Video_ID']}",
        "Platform": "TikTok",
        "Platform_Post_ID": master_row["Video_ID"],
        "Account_Username": config.TIKTOK_USERNAME,
        "Caption": master_row.get("Title"),
        "Publish_Date": master_row.get("Publish_Date"),
        "Permalink": master_row.get("Permalink"),
        "Post_Type": "Video",
        "Thumbnail_URL": None,
        "Duration": master_row.get("Duration"),
        "Views": master_row.get("Views"),
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Shares": master_row.get("Shares"),
        "Saves": None,  # not exposed by this scope
        "API_Status": master_row.get("API_Status"),
        "Last_Synced_At": master_row.get("Last_Synced_At"),
    }

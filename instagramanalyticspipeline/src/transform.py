"""
Normalizes raw Graph API responses into one Instagram_Master row per post.

Null vs. 0 convention (see docs/API_NOTES.md "Metric conventions"):
  - 0    : the API explicitly returned a value of 0 for that metric --
           i.e. we asked, and the real answer is "zero so far."
  - None : the metric was not returned at all (unsupported for this media
           type, insufficient data, or the request failed even after the
           per-metric fallback). We do NOT default missing data to 0,
           because that would misrepresent "unknown" as "no engagement."
"""
from datetime import datetime, timezone

IG_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"  # e.g. "2024-01-01T12:00:00+0000"


def _normalize_timestamp(raw: str):
    """IG returns offsets without a colon (+0000), which BigQuery's JSON
    loader can choke on. Re-emit as a standard ISO-8601 string."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, IG_TIMESTAMP_FORMAT).isoformat()
    except ValueError:
        return raw  # unexpected format -- pass through rather than drop the row


def parse_timestamp(raw: str):
    """Parses an IG timestamp into a timezone-aware datetime, or None if
    missing/unparseable. Used by pipeline.py to decide whether a post
    falls inside the insights-refresh window -- callers should treat None
    as "refresh it anyway" rather than silently skip a post we can't
    reliably date."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, IG_TIMESTAMP_FORMAT)
    except ValueError:
        return None

POST_TYPE_MAP = {
    "REELS": "Reel",
}


def _post_type(media_type: str, media_product_type: str) -> str:
    if media_product_type in POST_TYPE_MAP:
        return POST_TYPE_MAP[media_product_type]
    if media_type == "CAROUSEL_ALBUM":
        return "Carousel"
    if media_type == "VIDEO":
        return "Video"
    if media_type == "IMAGE":
        return "Image"
    return media_type or media_product_type or "Unknown"


def build_master_row(
    media_detail: dict,
    insights: dict,
    collaborators: list,
    account_info: dict,
) -> dict:
    """media_detail: result of graph_client.get_media_details()[post_id]
    insights: result of graph_client.get_media_insights()[post_id]
    collaborators: result of graph_client.get_collaborators(post_id)
    account_info: {"id":..., "username":..., "name":...} fetched once
    """
    insights = insights or {}

    # total_views/total_likes/total_comments aggregate across every
    # placement the post appears in, including paid/boosted distribution
    # (e.g. a partner running Partnership Ads on this content) -- see
    # docs/API_NOTES.md "Boosted/paid views". Fall back to the organic-only
    # metric/field if the total_ variant wasn't returned (e.g. an older API
    # version, or a media type that doesn't support it) rather than losing
    # the number entirely.
    views_organic = insights.get("views")
    total_views = insights.get("total_views")
    likes_organic = media_detail.get("like_count")
    total_likes = insights.get("total_likes")
    comments_organic = media_detail.get("comments_count")
    total_comments = insights.get("total_comments")

    return {
        "Post_ID": media_detail["id"],
        "Account_ID": account_info.get("id"),
        "Account_Username": account_info.get("username"),
        "Account_Name": account_info.get("name"),
        "Description": media_detail.get("caption"),  # None if caption empty
        "Duration": None,  # not exposed by the Graph API -- see docs/API_NOTES.md
        "Publish_Date": _normalize_timestamp(media_detail.get("timestamp")),
        "Permalink": media_detail.get("permalink"),
        "Post_Type": _post_type(
            media_detail.get("media_type"), media_detail.get("media_product_type")
        ),
        "Views": total_views if total_views is not None else views_organic,
        "Views_Organic": views_organic,
        "Reach": insights.get("reach"),
        "Likes": total_likes if total_likes is not None else likes_organic,
        "Shares": insights.get("shares"),
        "Follows": insights.get("follows"),
        "Comments": total_comments if total_comments is not None else comments_organic,
        "Saves": insights.get("saved"),
        "Total_Interactions": insights.get("total_interactions"),
        "Watch_Time": insights.get("ig_reels_video_view_total_time"),
        "Average_Watch_Time": insights.get("ig_reels_avg_watch_time"),
        "Tagged": None,  # not reliably available via API -- see Suggested_Partnership heuristic
        "Collabed": bool(collaborators),
        "Collaborator_Usernames": ",".join(collaborators) if collaborators else None,
        "Media_Audio_Type": media_detail.get("media_audio_type"),
        "Is_Shared_To_Feed": media_detail.get("is_shared_to_feed"),
        "API_Status": "Active",
        "Last_Synced_At": datetime.now(timezone.utc).isoformat(),
    }


def to_content_item(master_row: dict) -> dict:
    """Normalizes an Instagram_Master row into the shared cross-platform
    content_items shape (see shared/src/content_store.py). Called once per
    row, after build_master_row -- this never re-derives fields, only
    re-maps names/units so YouTube/TikTok/Facebook rows line up for
    matching and partner reporting."""
    return {
        "Content_ID": f"instagram:{master_row['Post_ID']}",
        "Platform": "Instagram",
        "Platform_Post_ID": master_row["Post_ID"],
        "Account_Username": master_row.get("Account_Username"),
        "Caption": master_row.get("Description"),
        "Publish_Date": master_row.get("Publish_Date"),
        "Permalink": master_row.get("Permalink"),
        "Post_Type": master_row.get("Post_Type"),
        "Thumbnail_URL": None,  # not fetched by this pipeline
        "Duration": master_row.get("Duration"),  # always None -- see docs/API_NOTES.md
        "Views": master_row.get("Views"),
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Shares": master_row.get("Shares"),
        "Saves": master_row.get("Saves"),
        "API_Status": master_row.get("API_Status"),
        "Last_Synced_At": master_row.get("Last_Synced_At"),
    }


def build_history_row(master_row: dict, snapshot_date: str) -> dict:
    """Built from an already-assembled Instagram_Master row so Likes/Comments
    (sourced from the media object, not /insights) stay consistent."""
    return {
        "Snapshot_Date": snapshot_date,
        "Post_ID": master_row["Post_ID"],
        "Views": master_row.get("Views"),
        "Views_Organic": master_row.get("Views_Organic"),
        "Reach": master_row.get("Reach"),
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Shares": master_row.get("Shares"),
        "Saves": master_row.get("Saves"),
        "Watch_Time": master_row.get("Watch_Time"),
        "Total_Interactions": master_row.get("Total_Interactions"),
    }

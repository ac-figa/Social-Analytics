"""
Normalizes raw YouTube Data API v3 responses into one YouTube_Master row
per video.

Same null vs. 0 convention as the other pipelines (see
instagramanalyticspipeline/src/transform.py's docstring): 0 means the API
explicitly returned zero, None means the field wasn't returned at all --
e.g. likeCount is absent (not 0) when a creator has hidden their like
count.
"""
import re
from datetime import datetime, timezone

_ISO8601_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_duration_seconds(iso_duration: str):
    """YouTube durations are ISO 8601 (e.g. 'PT43S', 'PT1M5S') -- convert
    to total seconds. Returns None if missing/unparseable."""
    if not iso_duration:
        return None
    match = _ISO8601_DURATION_RE.fullmatch(iso_duration)
    if not match:
        return None
    parts = match.groupdict()
    days = int(parts["days"] or 0)
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_timestamp(raw: str):
    """Returns a timezone-aware datetime, or None if missing/unparseable.
    Used by pipeline.py to decide whether a video falls inside the
    insights-refresh window."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _looks_like_short(iso_duration: str) -> bool:
    """YouTube's API doesn't label Shorts as a distinct type -- <=60s is
    the conventional heuristic (Shorts' original length limit). Not
    authoritative (Shorts can run longer now), just a best-effort tag."""
    seconds = parse_duration_seconds(iso_duration)
    return seconds is not None and seconds <= 60


def _to_int(value):
    """statistics fields come back as numeric strings, or absent entirely
    (e.g. a creator hiding their like count) -- never coerced to 0."""
    if value is None:
        return None
    return int(value)


def build_master_row(video: dict, channel_info: dict) -> dict:
    """video: one item from YouTubeClient.get_video_details()[video_id]
    channel_info: result of YouTubeClient.get_channel_info()
    """
    snippet = video.get("snippet", {})
    stats = video.get("statistics", {})
    content_details = video.get("contentDetails", {})
    duration_iso = content_details.get("duration")

    return {
        "Video_ID": video["id"],
        "Channel_ID": channel_info.get("id"),
        "Channel_Title": channel_info.get("title"),
        "Title": snippet.get("title"),
        "Description": snippet.get("description"),
        "Duration": parse_duration_seconds(duration_iso),
        "Publish_Date": snippet.get("publishedAt"),
        "Permalink": f"https://www.youtube.com/watch?v={video['id']}",
        "Post_Type": "Short" if _looks_like_short(duration_iso) else "Video",
        "Views": _to_int(stats.get("viewCount")),
        "Likes": _to_int(stats.get("likeCount")),
        "Comments": _to_int(stats.get("commentCount")),
        "Shares": None,  # not exposed by the YouTube Data API
        "Partnership": None,  # filled by pipeline.py from youtube_classifications
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
    }


def to_content_item(master_row: dict) -> dict:
    """Normalizes a YouTube_Master row into the shared cross-platform
    content_items shape (see shared/src/content_store.py)."""
    return {
        "Content_ID": f"youtube:{master_row['Video_ID']}",
        "Platform": "YouTube",
        "Platform_Post_ID": master_row["Video_ID"],
        "Account_Username": master_row.get("Channel_Title"),
        "Caption": master_row.get("Title"),
        "Publish_Date": master_row.get("Publish_Date"),
        "Permalink": master_row.get("Permalink"),
        "Post_Type": master_row.get("Post_Type"),
        "Thumbnail_URL": None,
        "Views": master_row.get("Views"),
        "Likes": master_row.get("Likes"),
        "Comments": master_row.get("Comments"),
        "Shares": master_row.get("Shares"),
        "Saves": None,  # not exposed by the YouTube Data API
        "API_Status": master_row.get("API_Status"),
        "Last_Synced_At": master_row.get("Last_Synced_At"),
    }

"""
Data access layer for the dashboard -- wraps shared/src/content_store.py
(the classification queue, pending matches, partnerships) and adds the
one piece that module doesn't own: propagating a group's classification
down into each member platform's own *_classifications table, so
instagram_master/facebook_master/youtube_master/tiktok_master (each
pipeline's own reporting surface) reflect it too, not just content_groups.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from google.cloud import bigquery  # noqa: E402

from shared.src import content_store  # noqa: E402

from . import config  # noqa: E402

UPDATED_BY = "webapp"


def get_client() -> bigquery.Client:
    return bigquery.Client(project=config.BQ_PROJECT_ID)


def ensure_schema(client: bigquery.Client) -> None:
    content_store.ensure_schema(client)


def _classifications_table_ref(platform: str) -> str:
    p = config.PLATFORM_CONFIG[platform]
    return f"{config.BQ_PROJECT_ID}.{p['dataset']}.{p['classifications_table']}"


def _propagate_to_platform(client: bigquery.Client, platform: str, platform_post_id: str, partnership: str, content_type: str) -> None:
    """Mirrors content_store.upsert_classification()'s MERGE shape -- see
    that function's four near-identical copies across each pipeline's own
    bigquery_store.py. Reimplemented here (rather than importing each
    pipeline's module) because every pipeline's config.py hard-requires
    its own API token env vars the dashboard has no reason to need."""
    p = config.PLATFORM_CONFIG[platform]
    table_ref = _classifications_table_ref(platform)
    id_column = p["id_column"]
    query = f"""
    MERGE `{table_ref}` T
    USING (SELECT @post_id AS {id_column}) S
    ON T.{id_column} = S.{id_column}
    WHEN MATCHED THEN UPDATE SET
      Partnership = @partnership, Content_Type = @content_type,
      Updated_At = CURRENT_TIMESTAMP(), Updated_By = @updated_by
    WHEN NOT MATCHED THEN INSERT ({id_column}, Partnership, Content_Type, Updated_At, Updated_By)
      VALUES (@post_id, @partnership, @content_type, CURRENT_TIMESTAMP(), @updated_by)
    """
    client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("post_id", "STRING", platform_post_id),
                bigquery.ScalarQueryParameter("partnership", "STRING", partnership),
                bigquery.ScalarQueryParameter("content_type", "STRING", content_type),
                bigquery.ScalarQueryParameter("updated_by", "STRING", UPDATED_BY),
            ]
        ),
    ).result()


def classify(client: bigquery.Client, group_id: str, content_id: str, platform: str, platform_post_id: str, partnership: str, content_type: str) -> str:
    """Classifies a group (or, if group_id is None, first creates a real
    one-member group for a previously-ungrouped item -- see
    content_store.list_classification_queue()'s docstring for why).
    Returns the Group_ID that ended up classified. Propagates the same
    Partnership/Content_Type to every member's own platform
    classifications table, not just content_groups -- a group's members
    are the same real-world content, so they always share one
    classification."""
    if not group_id:
        group_id = content_store.create_group(
            client,
            content_ids=[content_id],
            partnership=partnership,
            content_type=content_type,
            match_method="manual",
            confirmed=True,
            updated_by=UPDATED_BY,
        )
        member_platforms = [(platform, platform_post_id)]
    else:
        content_store.set_classification(client, group_id, partnership, content_type, UPDATED_BY)
        member_platforms = _member_platform_ids(client, group_id)

    for m_platform, m_post_id in member_platforms:
        _propagate_to_platform(client, m_platform, m_post_id, partnership, content_type)

    return group_id


def classify_bulk(client: bigquery.Client, rows: list) -> int:
    """rows: [{"group_id", "content_id", "platform", "platform_post_id",
    "partnership", "content_type"}] -- the webapp's "Apply All" button.
    Same effect as calling classify() once per row, but batched: one
    bulk group-creation pass, one bulk classification-update pass, and one
    MERGE per platform for propagation, instead of ~3 query jobs per row.
    Returns how many rows were applied."""
    if not rows:
        return 0

    new_rows = [r for r in rows if not r["group_id"]]
    existing_rows = [r for r in rows if r["group_id"]]

    if new_rows:
        new_group_ids = content_store.bulk_create_classified_groups(
            client,
            [{"content_id": r["content_id"], "partnership": r["partnership"], "content_type": r["content_type"]} for r in new_rows],
            updated_by=UPDATED_BY,
        )
        for r, gid in zip(new_rows, new_group_ids):
            r["group_id"] = gid

    if existing_rows:
        content_store.bulk_set_classifications(
            client,
            [{"Group_ID": r["group_id"], "Partnership": r["partnership"], "Content_Type": r["content_type"]} for r in existing_rows],
            updated_by=UPDATED_BY,
        )

    member_map = content_store.bulk_member_platform_ids(client, [r["group_id"] for r in existing_rows])

    by_platform: dict = {}
    for r in rows:
        members = member_map.get(r["group_id"]) or [(r["platform"], r["platform_post_id"])]
        for plat, post_id in members:
            by_platform.setdefault(plat, []).append(
                {"post_id": post_id, "partnership": r["partnership"], "content_type": r["content_type"]}
            )
    for plat, items in by_platform.items():
        content_store.propagate_bulk_classifications(client, plat, items, updated_by=UPDATED_BY)

    return len(rows)


def manual_group(client: bigquery.Client, selections: list) -> tuple:
    """selections: tokens of the form 'group:<Group_ID>' or 'item:<Content_ID>',
    from whatever a human checked across the Browse/Classify tables. Merges
    them all into one group -- reuses content_store.merge_groups() for any
    that are already in different groups, and add_members()/create_group()
    for raw ungrouped items. Returns (ok, message) for the caller to flash.
    Refuses (without writing anything) if the combined set would put two
    items from the same platform in one group, or if fewer than 2 distinct
    pieces of content were selected."""
    group_ids, content_ids = set(), set()
    for token in selections:
        kind, _, value = token.partition(":")
        if kind == "group" and value:
            group_ids.add(value)
        elif kind == "item" and value:
            content_ids.add(value)

    if len(group_ids) + len(content_ids) < 2:
        return False, "Select at least 2 items (from different platforms) to group."

    platforms_by_group = content_store.get_group_platforms(client, list(group_ids))
    items_info = content_store.get_platform_and_group_for_content_ids(client, list(content_ids))

    all_platforms: list = []
    for gid in group_ids:
        all_platforms.extend(platforms_by_group.get(gid, set()))
    for cid in content_ids:
        info = items_info.get(cid)
        if info:
            all_platforms.append(info["Platform"])

    seen, dupes = set(), set()
    for p in all_platforms:
        (dupes if p in seen else seen).add(p)
    if dupes:
        return False, f"Can't group two {'/'.join(sorted(dupes))} items together -- pick at most one per platform."

    if not group_ids:
        content_store.create_group(
            client, content_ids=list(content_ids), match_method="manual", confirmed=True, updated_by=UPDATED_BY,
        )
    else:
        keep_group_id = sorted(group_ids)[0]
        for other in group_ids:
            if other != keep_group_id:
                content_store.merge_groups(client, keep_group_id, other)
        if content_ids:
            content_store.add_members(
                client, keep_group_id, list(content_ids), match_method="manual", match_confidence=None, confirmed=True,
            )

    return True, f"Grouped {len(group_ids) + len(content_ids)} item(s) together."


def _member_platform_ids(client: bigquery.Client, group_id: str) -> list:
    query = f"""
    SELECT ci.Platform, ci.Platform_Post_ID
    FROM `{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.content_group_members` m
    JOIN `{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.content_items` ci ON m.Content_ID = ci.Content_ID
    WHERE m.Group_ID = @group_id
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("group_id", "STRING", group_id)]
        ),
    ).result()
    return [(r["Platform"], r["Platform_Post_ID"]) for r in rows]


def list_classification_queue(client: bigquery.Client, unclassified_only: bool = True, collabs_only: bool = False, limit: int = 100) -> list:
    groups = content_store.list_classification_queue(client, unclassified_only=unclassified_only, limit=limit if not collabs_only else 5000)
    if not collabs_only:
        return groups[:limit]

    collab_ids = _instagram_collab_post_ids(client)
    filtered = [
        g for g in groups
        if any(m["Platform"] == "Instagram" and m["Platform_Post_ID"] in collab_ids for m in g["Members"])
    ]
    return filtered[:limit]


def _instagram_collab_post_ids(client: bigquery.Client) -> set:
    ig = config.PLATFORM_CONFIG["Instagram"]
    query = f"SELECT Post_ID FROM `{config.BQ_PROJECT_ID}.{ig['dataset']}.instagram_master` WHERE Collabed = TRUE"
    return {r["Post_ID"] for r in client.query(query).result()}


def list_pending_matches(client: bigquery.Client, months: int = None) -> list:
    since = None
    if months is not None:
        since = datetime.now(timezone.utc) - timedelta(days=months * 30)
    return content_store.list_pending_matches(client, since=since)


def confirm_pending(client: bigquery.Client, group_id: str, content_id: str) -> None:
    """Accepting a pending match only ever flips that one membership's
    Confirmed flag -- but if the group it's joining was already classified
    (e.g. a TikTok match accepted into an Instagram/Facebook group that's
    already "Nike"), that classification needs to reach TikTok's own
    tiktok_classifications table too, or tiktok_master would keep showing
    Unclassified for a video that's really already classified via its
    group. content_store.confirm_membership() alone doesn't know about
    classification at all, so this has to happen here."""
    content_store.confirm_membership(client, group_id, content_id)

    classification = content_store.get_group_classification(client, group_id)
    if not classification:
        return
    partnership, content_type = classification
    if partnership in (None, "Unclassified"):
        return

    info = content_store.get_platform_and_group_for_content_ids(client, [content_id]).get(content_id)
    if not info:
        return
    post_id = content_id.split(":", 1)[1]
    content_store.propagate_bulk_classifications(
        client, info["Platform"], [{"post_id": post_id, "partnership": partnership, "content_type": content_type}],
        updated_by=UPDATED_BY,
    )


def reject_pending(client: bigquery.Client, group_id: str, content_id: str) -> None:
    content_store.remove_member(client, group_id, content_id)


def list_latest_items(client: bigquery.Client, platform: str, limit: int = 50, account: str = None) -> list:
    """The 50 most recently published content_items for one platform,
    with whether each one is currently in a group (and if so, which) --
    lets you see the raw synced data (caption/duration/date as the
    matcher actually sees them) next to real match outcomes, to spot why
    two platforms aren't linking up. account: optional Account_Username
    filter -- a platform can carry more than one account's posts (e.g.
    Instagram holding both the main page and Calcio Bros, all sharing
    this same table -- see instagramanalyticspipeline/src/config.py)."""
    account_clause = "AND ci.Account_Username = @account" if account else ""
    query = f"""
    SELECT
      ci.Content_ID, ci.Platform_Post_ID, ci.Account_Username, ci.Caption, ci.Publish_Date, ci.Duration,
      ci.Views, ci.Likes, ci.Comments, ci.Shares, ci.Permalink,
      m.Group_ID, m.Confirmed, g.Partnership, g.Content_Type
    FROM `{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.content_items` ci
    LEFT JOIN `{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.content_group_members` m
      ON ci.Content_ID = m.Content_ID
    LEFT JOIN `{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.content_groups` g
      ON m.Group_ID = g.Group_ID
    WHERE ci.Platform = @platform
    {account_clause}
    ORDER BY ci.Publish_Date DESC
    LIMIT @limit
    """
    params = [
        bigquery.ScalarQueryParameter("platform", "STRING", platform),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    if account:
        params.append(bigquery.ScalarQueryParameter("account", "STRING", account))
    rows = client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    return [dict(r) for r in rows]


def list_accounts_for_platform(client: bigquery.Client, platform: str) -> list:
    """Distinct Account_Username values synced for one platform -- lets
    Browse only show an Account filter when a platform actually carries
    more than one account's posts, instead of always showing one."""
    query = f"""
    SELECT DISTINCT Account_Username
    FROM `{config.BQ_PROJECT_ID}.{config.SHARED_BQ_DATASET}.content_items`
    WHERE Platform = @platform AND Account_Username IS NOT NULL
    ORDER BY Account_Username
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("platform", "STRING", platform)]
        ),
    ).result()
    return [r["Account_Username"] for r in rows]


def list_partnerships(client: bigquery.Client) -> list:
    """Each partnership plus its Video_Count, so the Partnerships page can
    show how many videos are under it without a click -- makes it obvious
    which ones are empty and safe to delete."""
    partnerships = content_store.list_partnerships(client)
    counts = content_store.get_partnership_video_counts(client)
    for p in partnerships:
        p["Video_Count"] = counts.get(p["Partnership"], 0)
    return partnerships


def add_partnership(client: bigquery.Client, partnership: str) -> None:
    content_store.add_partnership(client, partnership)


def delete_partnership(client: bigquery.Client, partnership: str) -> tuple:
    """Refuses (without deleting anything) if the partnership still has
    classified videos -- deleting the reference row wouldn't touch those
    groups' own Partnership field, so they'd silently vanish from the
    Partnerships list/dropdowns while still being "classified" under a
    name that no longer exists anywhere in the UI. Returns (ok, message)
    for the caller to flash."""
    counts = content_store.get_partnership_video_counts(client)
    count = counts.get(partnership, 0)
    if count > 0:
        return False, f"Can't delete '{partnership}' -- it still has {count} video(s) classified under it."
    content_store.delete_partnership(client, partnership)
    return True, f"Deleted '{partnership}'."


def add_content_type(client: bigquery.Client, partnership: str, content_type: str) -> None:
    content_store.add_content_type(client, partnership, content_type)


def get_partnership_report(client: bigquery.Client, partnership: str) -> dict:
    """Shapes content_store.get_partnership_groups() into what the
    per-partnership dashboard page needs: overall totals, a breakdown by
    Content_Type, and a breakdown by Platform (each group can carry stats
    for more than one platform), on top of the raw per-video list."""
    groups = content_store.get_partnership_groups(client, partnership)

    totals = {"Views": 0, "Likes": 0, "Comments": 0, "Shares": 0}
    content_type_counts: dict = {}
    platform_stats: dict = {}

    for g in groups:
        for key in totals:
            totals[key] += g.get(key) or 0
        content_type_counts[g["Content_Type"] or "Unclassified"] = content_type_counts.get(g["Content_Type"] or "Unclassified", 0) + 1
        for m in g["Members"]:
            stats = platform_stats.setdefault(m["Platform"], {"count": 0, "Views": 0, "Likes": 0, "Comments": 0, "Shares": 0})
            stats["count"] += 1
            for key in ("Views", "Likes", "Comments", "Shares"):
                stats[key] += m.get(key) or 0

    return {
        "groups": groups,
        "total_videos": len(groups),
        "totals": totals,
        "content_type_breakdown": sorted(content_type_counts.items(), key=lambda kv: -kv[1]),
        "platform_breakdown": sorted(platform_stats.items(), key=lambda kv: kv[0]),
    }


def _account_brand(account_username: str) -> str:
    """Classifies an account_stats row by brand for the Media Kit's
    filter -- a substring match on Account_Username rather than a stored
    Brand column, since only two brands exist right now and Instagram/
    TikTok/etc. handles for the same brand aren't guaranteed to be
    spelled identically. Everything not matched defaults to Bello Bros."""
    if account_username and "calcio" in account_username.lower():
        return "Calcio Bros"
    return "Bello Bros"


def get_media_kit(client: bigquery.Client, brand: str = None) -> dict:
    """Media Kit page data: one card per account with latest follower/
    subscriber count (from the daily account_stats snapshots) plus Views
    in the last 30/90/270 days (computed live from content_items -- see
    content_store.get_views_in_window()'s docstring for why that's not
    stored), and an aggregate total across the accounts shown -- every
    platform summed together, the one number a brand pitch opens with.
    brand: optional filter (e.g. "Calcio Bros") -- when set, only that
    brand's accounts are returned and totals are scoped to match, so the
    combined total on screen always matches what's actually shown below
    it. Views window days are hardcoded here rather than made
    configurable -- exactly what a media kit for brands conventionally
    reports, no reason to expose more knobs than that."""
    accounts = content_store.get_latest_account_stats(client)
    for a in accounts:
        a["Brand"] = _account_brand(a["Account_Username"])
    available_brands = sorted({a["Brand"] for a in accounts})

    if brand:
        accounts = [a for a in accounts if a["Brand"] == brand]

    windows = (30, 90, 270)
    for a in accounts:
        for days in windows:
            a[f"Views_{days}d"] = content_store.get_views_in_window(
                client, a["Platform"], a["Account_Username"], days
            )

    totals = {"Followers": 0, "Views_30d": 0, "Views_90d": 0, "Views_270d": 0}
    for a in accounts:
        totals["Followers"] += a.get("Followers") or 0
        for days in windows:
            totals[f"Views_{days}d"] += a.get(f"Views_{days}d") or 0

    return {"accounts": accounts, "totals": totals, "brands": available_brands}


_STORY_INT_FIELDS = {"Views", "Likes", "Shares", "Sticker_Taps", "Replies"}


def _coerce_story_fields(raw: dict) -> dict:
    """Normalizes a story row of form-submitted strings into BigQuery-
    ready types: blank -> None (so an empty numeric field stores as NULL,
    not an error), numeric fields -> int, everything else passed through
    trimmed."""
    result = {}
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        if value == "":
            result[key] = None
            continue
        if key in _STORY_INT_FIELDS:
            try:
                result[key] = int(value)
            except (TypeError, ValueError):
                result[key] = None
        else:
            result[key] = value
    return result


def list_stories(client: bigquery.Client) -> list:
    return content_store.list_stories(client)


def add_stories(client: bigquery.Client, rows: list) -> int:
    """rows: raw form-submitted dicts from the Stories tab's Submit All
    button. Coerces types and registers any new Partnership/Content_Type
    combination, same as classify_bulk()."""
    clean_rows = [_coerce_story_fields(r) for r in rows]
    count = content_store.bulk_create_stories(client, clean_rows)
    for r in clean_rows:
        if r.get("Partnership"):
            add_content_type(client, r["Partnership"], r.get("Content_Type") or "Unclassified")
    return count


def update_story(client: bigquery.Client, story_id: str, fields: dict) -> None:
    clean = _coerce_story_fields(fields)
    content_store.update_story(client, story_id, clean)
    if clean.get("Partnership"):
        add_content_type(client, clean["Partnership"], clean.get("Content_Type") or "Unclassified")


def delete_story(client: bigquery.Client, story_id: str) -> None:
    content_store.delete_story(client, story_id)

"""
Classification dashboard. Runs locally (python3 app.py) or deployed to
Cloud Run behind Google sign-in (see deploy/README.md) -- same codebase,
same routes either way.

Run locally with the same Python environment used for the pipelines (it
needs google-cloud-bigquery, flask, python-dotenv):

  python3 app.py

Then open http://127.0.0.1:5050 -- see README.md for full setup.
"""
import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from src import config, db, sync, vision

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
# Cloud Run terminates HTTPS and proxies plain HTTP to the container --
# without this, Flask thinks every request is http:// (wrong scheme for
# the OAuth redirect_uri, and for any generated absolute URL). No-op when
# running locally with no proxy in front.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.jinja_env.filters["compact"] = db.compact_number

# Auth is only enforced when GOOGLE_CLIENT_ID is actually configured --
# running locally (webapp/README.md's setup) never sets it, so local dev
# stays login-free. See src/auth.py and deploy/README.md.
_AUTH_ENABLED = bool(config.GOOGLE_CLIENT_ID)
if _AUTH_ENABLED:
    from src import auth

    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    auth.oauth.init_app(app)

    @app.before_request
    def _require_login():
        # public_share is the whole point of this feature: a link brands
        # can open without a Google account on ALLOWED_EMAILS. Everything
        # else on this site still requires internal sign-in.
        if request.endpoint in ("login", "login_start", "auth_callback", "static", "public_share", "public_topic_share") or auth.is_logged_in():
            return None
        return redirect(url_for("login"))

    @app.route("/login")
    def login():
        return render_template("login.html", denied=False)

    @app.route("/login/google")
    def login_start():
        redirect_uri = url_for("auth_callback", _external=True)
        return auth.google.authorize_redirect(redirect_uri)

    @app.route("/auth/callback")
    def auth_callback():
        email = auth.handle_callback()
        if email is None:
            return render_template("login.html", denied=True), 403
        return redirect(url_for("queue"))

    @app.route("/logout")
    def logout():
        auth.logout()
        return redirect(url_for("login"))

# Runs once at import time so it works both for local `python3 app.py`
# and for gunicorn importing `app:app` directly on Cloud Run (which never
# executes the __main__ block below).
db.ensure_schema(db.get_client())


@app.route("/")
def queue():
    client = db.get_client()
    unclassified_only = request.args.get("all") != "1"
    collabs_only = request.args.get("collabs") == "1"
    limit = request.args.get("limit", default=100, type=int)
    if limit not in (100, 250, 500, 1000):
        limit = 100
    groups = db.list_classification_queue(client, unclassified_only=unclassified_only, collabs_only=collabs_only, limit=limit)
    partnerships = db.list_partnerships(client)
    partnership_content_types = {p["Partnership"]: p["Content_Types"] for p in partnerships}
    return render_template(
        "queue.html",
        groups=groups,
        partnerships=partnerships,
        partnership_content_types=partnership_content_types,
        all_topics=[t["Topic"] for t in db.list_topics(client)],
        unclassified_only=unclassified_only,
        collabs_only=collabs_only,
        limit=limit,
        nav_counts=db.get_dashboard_counts(client),
    )


@app.route("/classify", methods=["POST"])
def classify():
    client = db.get_client()
    group_id = request.form.get("group_id") or None
    content_id = request.form.get("content_id") or None
    platform = request.form.get("platform") or None
    platform_post_id = request.form.get("platform_post_id") or None

    if request.form.get("action") == "organic":
        # One-click "not a partnership" classification -- Organic is just a
        # regular partnership entry (so it shows up in reporting/filters
        # like any other), the quick button just skips typing it in.
        partnership = "Organic"
        content_type = request.form.get("content_type", "").strip() or "Organic"
    else:
        partnership = request.form.get("partnership", "").strip()
        content_type = request.form.get("content_type", "").strip() or "Unclassified"

    if partnership:
        group_id = db.classify(client, group_id, content_id, platform, platform_post_id, partnership, content_type)
        db.add_content_type(client, partnership, content_type)

    # Topics only ever tag a real group -- on a still-ungrouped item with
    # no Partnership given, there's nothing to attach them to yet (see
    # db.set_group_topics()'s docstring). Classifying it first (even just
    # as Organic) creates that group; Topics can be added on the next Save.
    topics_raw = request.form.get("topics")
    if group_id and topics_raw is not None:
        topics = [t.strip() for t in topics_raw.split(",") if t.strip()]
        db.set_group_topics(client, group_id, topics)

    return redirect(request.referrer or url_for("queue"))


@app.route("/classify/bulk", methods=["POST"])
def classify_bulk():
    """The "Apply All" button -- app.js gathers every filled-in row from
    the classify queue client-side and POSTs them all here in one request,
    instead of one page-reloading form submit per row."""
    client = db.get_client()
    payload = request.get_json(silent=True) or {}
    rows = []
    for r in payload.get("rows", []):
        partnership = (r.get("partnership") or "").strip()
        if not partnership:
            continue
        rows.append(
            {
                "group_id": r.get("group_id") or None,
                "content_id": r.get("content_id") or None,
                "platform": r.get("platform") or None,
                "platform_post_id": r.get("platform_post_id") or None,
                "partnership": partnership,
                "content_type": (r.get("content_type") or "").strip() or "Unclassified",
            }
        )

    applied = db.classify_bulk(client, rows) if rows else 0
    for r in rows:
        db.add_content_type(client, r["partnership"], r["content_type"])

    return jsonify({"applied": applied})


@app.route("/group", methods=["POST"])
def group_selected():
    client = db.get_client()
    selections = request.form.getlist("selections")
    ok, message = db.manual_group(client, selections)
    flash(message, "success" if ok else "error")
    return redirect(request.referrer or url_for("queue"))


@app.route("/partnerships")
def partnerships():
    client = db.get_client()
    return render_template(
        "partnerships.html", partnerships=db.list_partnerships(client), nav_counts=db.get_dashboard_counts(client)
    )


@app.route("/partnerships/add", methods=["POST"])
def add_partnership():
    name = request.form.get("partnership", "").strip()
    if name:
        db.add_partnership(db.get_client(), name)
    return redirect(url_for("partnerships"))


@app.route("/partnerships/delete", methods=["POST"])
def delete_partnership():
    name = request.form.get("partnership", "").strip()
    if name:
        ok, message = db.delete_partnership(db.get_client(), name)
        flash(message, "success" if ok else "error")
    return redirect(url_for("partnerships"))


@app.route("/partnerships/<partnership>")
def partnership_detail(partnership):
    client = db.get_client()
    report = db.get_partnership_report(client, partnership)
    return render_template(
        "partnership_detail.html", partnership=partnership, report=report,
        all_topics=[t["Topic"] for t in db.list_topics(client)],
        nav_counts=db.get_dashboard_counts(client),
    )


@app.route("/partnerships/<partnership>/share", methods=["POST"])
def get_share_link(partnership):
    client = db.get_client()
    token = db.get_share_token(client, partnership)
    link = url_for("public_share", token=token, _external=True)
    flash(f"Share link for {partnership} (anyone with this link can view it): {link}", "success")
    return redirect(url_for("partnership_detail", partnership=partnership))


@app.route("/partnerships/<partnership>/apply-topic", methods=["POST"])
def apply_topic_to_partnership(partnership):
    topic = request.form.get("topic", "").strip()
    if topic:
        client = db.get_client()
        count = db.apply_topic_to_partnership(client, partnership, topic)
        flash(f'Tagged {count} video(s) in "{partnership}" with topic "{topic}".', "success")
    return redirect(url_for("partnership_detail", partnership=partnership))


@app.route("/share/<token>")
def public_share(token):
    client = db.get_client()
    partnership = db.get_partnership_by_share_token(client, token)
    if partnership is None:
        return render_template("share_not_found.html"), 404
    report = db.get_partnership_report(client, partnership)
    return render_template("share.html", partnership=partnership, report=report)


@app.route("/topics")
def topics():
    client = db.get_client()
    return render_template("topics.html", topics=db.list_topics(client), nav_counts=db.get_dashboard_counts(client))


@app.route("/topics/add", methods=["POST"])
def add_topic():
    name = request.form.get("topic", "").strip()
    if name:
        db.add_topic(db.get_client(), name)
    return redirect(url_for("topics"))


@app.route("/topics/delete", methods=["POST"])
def delete_topic():
    name = request.form.get("topic", "").strip()
    if name:
        db.delete_topic(db.get_client(), name)
        flash(f"Deleted topic '{name}'.", "success")
    return redirect(url_for("topics"))


@app.route("/topics/<topic>")
def topic_detail(topic):
    client = db.get_client()
    months = request.args.get("months", default=12, type=int)
    if months <= 0:
        months = None
    report = db.get_topic_report(client, topic, months=months)
    return render_template(
        "topic_detail.html", topic=topic, report=report, months=months, nav_counts=db.get_dashboard_counts(client)
    )


@app.route("/topics/<topic>/share", methods=["POST"])
def get_topic_share_link(topic):
    client = db.get_client()
    token = db.get_topic_share_token(client, topic)
    link = url_for("public_topic_share", token=token, _external=True)
    flash(f'Share link for topic "{topic}" (anyone with this link can view it): {link}', "success")
    return redirect(url_for("topic_detail", topic=topic))


@app.route("/topic-share/<token>")
def public_topic_share(token):
    client = db.get_client()
    topic = db.get_topic_by_share_token(client, token)
    if topic is None:
        return render_template("share_not_found.html"), 404
    report = db.get_topic_report(client, topic)
    return render_template("topic_share.html", topic=topic, report=report)


@app.route("/partnerships/content-types/add", methods=["POST"])
def add_content_type():
    partnership = request.form.get("partnership", "").strip()
    content_type = request.form.get("content_type", "").strip()
    if partnership and content_type:
        db.add_content_type(db.get_client(), partnership, content_type)
    return redirect(url_for("partnerships"))


PLATFORMS = ["Instagram", "Facebook", "YouTube", "TikTok"]


@app.route("/browse")
def browse():
    platform = request.args.get("platform", "Instagram")
    if platform not in PLATFORMS:
        platform = "Instagram"
    client = db.get_client()
    accounts = db.list_accounts_for_platform(client, platform)
    account = request.args.get("account") or None
    if account not in accounts:
        account = None
    items = db.list_latest_items(client, platform, limit=50, account=account)
    partnerships = db.list_partnerships(client)
    partnership_content_types = {p["Partnership"]: p["Content_Types"] for p in partnerships}
    return render_template(
        "browse.html", items=items, platform=platform, platforms=PLATFORMS, accounts=accounts, account=account,
        partnerships=partnerships, partnership_content_types=partnership_content_types,
        all_topics=[t["Topic"] for t in db.list_topics(client)],
        nav_counts=db.get_dashboard_counts(client),
    )


@app.route("/pending")
def pending():
    client = db.get_client()
    months = request.args.get("months", default=9, type=int)
    if months <= 0:
        months = None
    matches = db.list_pending_matches(client, months=months)
    return render_template("pending.html", matches=matches, months=months, nav_counts=db.get_dashboard_counts(client))


@app.route("/pending/confirm", methods=["POST"])
def confirm_pending():
    db.confirm_pending(db.get_client(), request.form["group_id"], request.form["content_id"])
    return redirect(url_for("pending"))


@app.route("/pending/reject", methods=["POST"])
def reject_pending():
    db.reject_pending(db.get_client(), request.form["group_id"], request.form["content_id"])
    return redirect(url_for("pending"))


@app.route("/media-kit")
def media_kit():
    client = db.get_client()
    brand = request.args.get("brand") or None
    media_kit_data = db.get_media_kit(client, brand=brand)
    return render_template(
        "media_kit.html",
        accounts=media_kit_data["accounts"],
        totals=media_kit_data["totals"],
        brands=media_kit_data["brands"],
        brand=brand,
        demographics_upload_available=bool(config.ANTHROPIC_API_KEY),
        nav_counts=db.get_dashboard_counts(client),
    )


_DEMOGRAPHICS_UPLOAD_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@app.route("/media-kit/demographics/upload", methods=["POST"])
def upload_demographics_screenshot():
    if not config.ANTHROPIC_API_KEY:
        flash("Screenshot uploads aren't configured on this deployment (missing ANTHROPIC_API_KEY).", "error")
        return redirect(url_for("media_kit"))

    platform = request.form.get("platform", "").strip()
    account_username = request.form.get("account_username", "").strip()
    file = request.files.get("screenshot")
    if not platform or not account_username or not file or not file.filename:
        flash("Missing platform, account, or screenshot file.", "error")
        return redirect(url_for("media_kit"))
    if file.mimetype not in _DEMOGRAPHICS_UPLOAD_MEDIA_TYPES:
        flash(f"Unsupported image type: {file.mimetype}. Use PNG, JPEG, WebP, or GIF.", "error")
        return redirect(url_for("media_kit"))

    try:
        summary = db.record_demographics_from_screenshot(
            db.get_client(), platform, account_username, file.read(), file.mimetype
        )
        flash(summary, "success")
    except vision.VisionParseError as e:
        flash(f"Couldn't read that screenshot: {e}", "error")

    return redirect(url_for("media_kit"))


_EXPORT_BRANDS = ("Bello Bros", "Calcio Bros")


@app.route("/export")
def export():
    client = db.get_client()

    range_key = request.args.get("range", "30")
    now = datetime.now(timezone.utc)
    since = until = None
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")
    if range_key == "all":
        pass
    elif range_key == "custom":
        if from_date:
            since = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if to_date:
            until = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    else:
        try:
            days = int(range_key)
        except ValueError:
            days, range_key = 30, "30"
        since = now - timedelta(days=days)

    all_partnerships = [p["Partnership"] for p in db.list_partnerships(client)]
    all_topics = [t["Topic"] for t in db.list_topics(client)]
    all_accounts = db.list_all_accounts(client)
    valid_account_usernames = {a["Account_Username"] for a in all_accounts}
    platforms = [p for p in request.args.getlist("platforms") if p in PLATFORMS]
    accounts = [a for a in request.args.getlist("accounts") if a in valid_account_usernames]
    partnerships = [p for p in request.args.getlist("partnerships") if p in all_partnerships]
    topics = [t for t in request.args.getlist("topics") if t in all_topics]
    brand = request.args.get("brand") or None
    if brand not in _EXPORT_BRANDS:
        brand = None
    group_by = request.args.get("group_by", "none")

    report = db.get_export_report(
        client, since=since, until=until,
        platforms=platforms or None, accounts=accounts or None,
        partnerships=partnerships or None, topics=topics or None,
        brand=brand, group_by=group_by,
    )

    if request.args.get("format") == "csv":
        return _export_csv_response(report, group_by)

    if range_key == "all":
        range_label = "All time"
    elif range_key == "custom":
        range_label = f"{from_date or '…'} – {to_date or '…'}"
    else:
        range_label = f"Last {range_key} days"

    partnership_options = [(p, p) for p in all_partnerships]
    topic_options = [(t, t) for t in all_topics]
    account_options = [(a["Account_Username"], f'{a["Account_Username"]} ({a["Platform"]})') for a in all_accounts]

    return render_template(
        "export.html",
        report=report,
        platforms=PLATFORMS, selected_platforms=platforms,
        account_options=account_options, selected_accounts=accounts,
        partnership_options=partnership_options, selected_partnerships=partnerships,
        topic_options=topic_options, selected_topics=topics,
        brands=_EXPORT_BRANDS, brand=brand,
        range_key=range_key, range_label=range_label, from_date=from_date, to_date=to_date, group_by=group_by,
        nav_counts=db.get_dashboard_counts(client),
    )


def _export_csv_response(report: dict, group_by: str) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    totals = report["totals"]
    if group_by == "none":
        writer.writerow(["Metric", "Value"])
        for key, label in (
            ("Post_Count", "Posts"), ("Views", "Views"), ("Likes", "Likes"),
            ("Comments", "Comments"), ("Shares", "Shares"), ("Followers", "Followers"),
        ):
            writer.writerow([label, totals[key]])
    else:
        writer.writerow([group_by.capitalize(), "Posts", "Views", "Likes", "Comments", "Shares", "Followers"])
        for row in report["breakdown"]:
            writer.writerow([row["Label"], row["Post_Count"], row["Views"], row["Likes"], row["Comments"], row["Shares"], row["Followers"]])
        writer.writerow(["Total", totals["Post_Count"], totals["Views"], totals["Likes"], totals["Comments"], totals["Shares"], totals["Followers"]])
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=social-analytics-export.csv"},
    )


@app.route("/stories")
def stories():
    client = db.get_client()
    partnerships = db.list_partnerships(client)
    return render_template(
        "stories.html",
        stories=db.list_stories(client),
        platforms=PLATFORMS,
        partnerships=partnerships,
        partnership_content_types={p["Partnership"]: p["Content_Types"] for p in partnerships},
        nav_counts=db.get_dashboard_counts(client),
    )


@app.route("/stories/add", methods=["POST"])
def add_stories():
    client = db.get_client()
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows", [])
    # Skip fully-blank rows -- "+ Add Row" starts empty, and not every
    # added row necessarily gets filled in before Submit All.
    rows = [r for r in rows if any((r.get(k) or "").strip() for k in ("Caption", "Views", "Platform"))]
    added = db.add_stories(client, rows) if rows else 0
    return jsonify({"added": added})


@app.route("/stories/update", methods=["POST"])
def update_story():
    client = db.get_client()
    story_id = request.form.get("story_id")
    fields = {
        col: request.form.get(col)
        for col in (
            "Platform", "Account_Username", "Caption", "Publish_Date", "Views", "Likes",
            "Shares", "Sticker_Taps", "Replies", "Tagged", "Partnership", "Content_Type",
        )
    }
    if story_id:
        db.update_story(client, story_id, fields)
    return redirect(url_for("stories"))


@app.route("/stories/delete", methods=["POST"])
def delete_story():
    client = db.get_client()
    story_id = request.form.get("story_id")
    if story_id:
        db.delete_story(client, story_id)
    return redirect(url_for("stories"))


@app.route("/sync")
def sync_page():
    return render_template(
        "sync.html", status=sync.get_status(), sync_available=config.SYNC_AVAILABLE,
        nav_counts=db.get_dashboard_counts(db.get_client()),
    )


@app.route("/sync/start", methods=["POST"])
def sync_start():
    if config.SYNC_AVAILABLE:
        sync.start_sync()
    return redirect(url_for("sync_page"))


@app.route("/sync/status")
def sync_status():
    return sync.get_status()


if __name__ == "__main__":
    # debug=True is safe here -- host="127.0.0.1" means this is never
    # reachable from outside your machine, and it shows the real error
    # (with a traceback) in the browser instead of a generic 500 page.
    app.run(host="127.0.0.1", port=5050, debug=True)

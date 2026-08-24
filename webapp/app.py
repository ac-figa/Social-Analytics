"""
Classification dashboard. Runs locally (python3 app.py) or deployed to
Cloud Run behind Google sign-in (see deploy/README.md) -- same codebase,
same routes either way.

Run locally with the same Python environment used for the pipelines (it
needs google-cloud-bigquery, flask, python-dotenv):

  python3 app.py

Then open http://127.0.0.1:5050 -- see README.md for full setup.
"""
from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from src import config, db, sync

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
# Cloud Run terminates HTTPS and proxies plain HTTP to the container --
# without this, Flask thinks every request is http:// (wrong scheme for
# the OAuth redirect_uri, and for any generated absolute URL). No-op when
# running locally with no proxy in front.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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
        if request.endpoint in ("login", "login_start", "auth_callback", "static") or auth.is_logged_in():
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
    groups = db.list_classification_queue(client, unclassified_only=unclassified_only, collabs_only=collabs_only)
    partnerships = db.list_partnerships(client)
    partnership_content_types = {p["Partnership"]: p["Content_Types"] for p in partnerships}
    return render_template(
        "queue.html",
        groups=groups,
        partnerships=partnerships,
        partnership_content_types=partnership_content_types,
        unclassified_only=unclassified_only,
        collabs_only=collabs_only,
    )


@app.route("/classify", methods=["POST"])
def classify():
    client = db.get_client()
    group_id = request.form.get("group_id") or None
    content_id = request.form.get("content_id") or None
    platform = request.form.get("platform") or None
    platform_post_id = request.form.get("platform_post_id") or None
    partnership = request.form.get("partnership", "").strip()
    content_type = request.form.get("content_type", "").strip() or "Unclassified"

    if partnership:
        db.classify(client, group_id, content_id, platform, platform_post_id, partnership, content_type)
        db.add_content_type(client, partnership, content_type)

    return redirect(request.referrer or url_for("queue"))


@app.route("/partnerships")
def partnerships():
    client = db.get_client()
    return render_template("partnerships.html", partnerships=db.list_partnerships(client))


@app.route("/partnerships/add", methods=["POST"])
def add_partnership():
    name = request.form.get("partnership", "").strip()
    if name:
        db.add_partnership(db.get_client(), name)
    return redirect(url_for("partnerships"))


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
    items = db.list_latest_items(client, platform, limit=50)
    return render_template("browse.html", items=items, platform=platform, platforms=PLATFORMS)


@app.route("/pending")
def pending():
    client = db.get_client()
    months = request.args.get("months", default=9, type=int)
    if months <= 0:
        months = None
    matches = db.list_pending_matches(client, months=months)
    return render_template("pending.html", matches=matches, months=months)


@app.route("/pending/confirm", methods=["POST"])
def confirm_pending():
    db.confirm_pending(db.get_client(), request.form["group_id"], request.form["content_id"])
    return redirect(url_for("pending"))


@app.route("/pending/reject", methods=["POST"])
def reject_pending():
    db.reject_pending(db.get_client(), request.form["group_id"], request.form["content_id"])
    return redirect(url_for("pending"))


@app.route("/sync")
def sync_page():
    return render_template("sync.html", status=sync.get_status(), sync_available=config.SYNC_AVAILABLE)


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

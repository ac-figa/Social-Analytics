"""
Local classification dashboard.

Run with the same Python environment used for the pipelines (it needs
google-cloud-bigquery, flask, python-dotenv):

  python3 app.py

Then open http://127.0.0.1:5050 -- see README.md for full setup.
"""
from flask import Flask, redirect, render_template, request, url_for

from src import config, db, sync

app = Flask(__name__)


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


@app.route("/pending")
def pending():
    client = db.get_client()
    return render_template("pending.html", matches=db.list_pending_matches(client))


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
    return render_template("sync.html", status=sync.get_status())


@app.route("/sync/start", methods=["POST"])
def sync_start():
    sync.start_sync()
    return redirect(url_for("sync_page"))


@app.route("/sync/status")
def sync_status():
    return sync.get_status()


if __name__ == "__main__":
    db.ensure_schema(db.get_client())
    # debug=True is safe here -- host="127.0.0.1" means this is never
    # reachable from outside your machine, and it shows the real error
    # (with a traceback) in the browser instead of a generic 500 page.
    app.run(host="127.0.0.1", port=5050, debug=True)

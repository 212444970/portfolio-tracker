import os
import logging
import webbrowser
from functools import wraps

import requests
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, redirect, render_template, request, session, url_for, Response

import historical_store
import strava_archive_store
import garmin_archive_store
import garmin_client
import strava_client

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

PORT = int(os.environ.get("PORT", 5000))
APP_URL = os.environ.get("APP_URL", f"http://localhost:{PORT}")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
DATA_SOURCE = os.environ.get("DATA_SOURCE", "mixed").lower()  # "mixed" or "strava"
log = logging.getLogger(__name__)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if ADMIN_PASSWORD and not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def _load_all_activities():
    historical = historical_store.load()
    archived = [
        activity
        for activity in strava_archive_store.load()
        if activity["date"] < strava_archive_store.GARMIN_CUTOFF
    ]
    garmin_arc = garmin_archive_store.load()
    garmin     = garmin_client.get_activities()
    combined = historical + archived + garmin_arc + garmin
    return sorted(combined, key=lambda x: x["date"], reverse=True)


@app.route("/")
def index():
    if DATA_SOURCE == "strava":
        historical = historical_store.load()
        try:
            strava = strava_client.get_activities()
        except requests.RequestException:
            log.exception("Failed to fetch Strava activities; showing historical data only")
            strava = []
        if strava is None:
            return render_template("index.html", connected=False)
        activities = sorted(historical + strava, key=lambda x: x["date"], reverse=True)
        return render_template(
            "index.html",
            connected=True,
            activities=activities,
            category_labels=strava_client.CATEGORY_LABELS,
        )

    if not garmin_client.is_connected() and not strava_archive_store.load() and not historical_store.load():
        return render_template("index.html", connected=False)
    activities = _load_all_activities()
    return render_template(
        "index.html",
        connected=True,
        activities=activities,
        category_labels=garmin_client.CATEGORY_LABELS,
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        error = "Wrong password."
    return render_template("admin_login.html", error=error)


@app.route("/admin")
@admin_required
def admin():
    return render_template(
        "admin.html",
        garmin_connected=garmin_client.is_connected(),
        archive_count=len(strava_archive_store.load()),
        garmin_archive_count=len(garmin_archive_store.load()),
    )


@app.route("/admin/garmin-disconnect", methods=["POST"])
@admin_required
def admin_garmin_disconnect():
    garmin_client.disconnect()
    return redirect(url_for("admin"))


@app.route("/admin/refresh", methods=["POST"])
@admin_required
def admin_refresh():
    garmin_client.clear_cache()
    return redirect(url_for("admin"))


@app.route("/admin/upload-garmin-tokens", methods=["POST"])
@admin_required
def admin_upload_garmin_tokens():
    import json
    f = request.files.get("tokens_file")
    if not f:
        return "Žádný soubor.", 400
    try:
        export = json.load(f)
    except Exception:
        return "Neplatný JSON soubor.", 400
    data_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(data_dir, exist_ok=True)
    tokens = export.get("garmin_tokens.json") if isinstance(export, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("di_token") or not tokens.get("di_refresh_token"):
        return "Soubor neobsahuje žádné tokeny.", 400
    path = os.path.join(data_dir, "garmin_tokens.json")
    with open(path, "w", encoding="utf-8") as out:
        json.dump(tokens, out)
    garmin_client.reset_client()
    return redirect(url_for("admin"))


@app.route("/admin/export-strava")
@admin_required
def admin_export_strava():
    """Download all Strava activities as strava_archive.json (run on Railway where tokens exist)."""
    import json, token_store
    tokens = token_store.load()
    if not tokens:
        return "Strava token not found on this server.", 503
    activities = strava_client.get_activities(force=True)
    if not activities:
        return "No activities returned from Strava.", 503
    CUTOFF = "2026-08-16"
    archived = [a for a in activities if a["date"] < CUTOFF]
    data = json.dumps(archived, ensure_ascii=False, indent=2)
    return Response(
        data,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=strava_archive.json"},
    )


if __name__ == "__main__":
    webbrowser.open(f"http://localhost:{PORT}")
    app.run(port=PORT, debug=False)

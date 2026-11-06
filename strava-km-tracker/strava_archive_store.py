import json
import os

_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_FILE = os.path.join(_DATA_DIR, "strava_archive.json")
_FALLBACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strava_archive.json")
GARMIN_CUTOFF = "2026-08-16"


def load():
    path = ARCHIVE_FILE if os.path.exists(ARCHIVE_FILE) else _FALLBACK
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

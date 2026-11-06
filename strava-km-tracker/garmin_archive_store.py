import json
import os

_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_FILE = os.path.join(_DATA_DIR, "garmin_archive.json")
_FALLBACK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garmin_archive.json")


def load():
    path = ARCHIVE_FILE if os.path.exists(ARCHIVE_FILE) else _FALLBACK
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

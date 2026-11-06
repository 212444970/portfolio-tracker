import json
import os

HISTORICAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_data.json")
HISTORICAL_CUTOFF = "2024-01-01"  # historical data covers everything before this date


def load():
    """Return list of synthetic monthly activities from the historical JSON file."""
    try:
        with open(HISTORICAL_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []

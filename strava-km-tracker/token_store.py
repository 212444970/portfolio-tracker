import json
import os

# On Railway: set DATA_DIR=/data and mount a volume there so tokens survive restarts.
# Locally: defaults to the project directory.
_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(_DATA_DIR, "tokens.json")


def load():
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(data):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)


def delete():
    try:
        os.remove(TOKEN_FILE)
    except FileNotFoundError:
        pass

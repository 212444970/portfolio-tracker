"""
Static site builder for Netlify.

Reads STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN from env,
fetches all rides, and writes public/index.html.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
RIDE_TYPES = {"Ride", "VirtualRide", "EBikeRide", "GravelRide", "MountainBikeRide"}


# ---------------------------------------------------------------------------
# Strava helpers
# ---------------------------------------------------------------------------

def get_access_token():
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_all_rides(access_token):
    rides = []
    page = 1
    while True:
        resp = requests.get(
            STRAVA_ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": 200, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        for a in batch:
            if a.get("sport_type") in RIDE_TYPES and a.get("distance", 0) > 0:
                rides.append({
                    "name": a["name"],
                    "date": a["start_date_local"][:10],
                    "km": round(a["distance"] / 1000, 2),
                    "type": a["sport_type"],
                    "moving_time": a["moving_time"],
                    "elevation": round(a.get("total_elevation_gain", 0)),
                })
        if len(batch) < 200:
            break
        page += 1
    return sorted(rides, key=lambda x: x["date"], reverse=True)


def compute_stats(rides):
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def parse(d):
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)

    return {
        "total": round(sum(r["km"] for r in rides), 1),
        "this_year": round(sum(r["km"] for r in rides if parse(r["date"]) >= year_start), 1),
        "this_month": round(sum(r["km"] for r in rides if parse(r["date"]) >= month_start), 1),
        "this_week": round(sum(r["km"] for r in rides if parse(r["date"]) >= week_start), 1),
        "ride_count": len(rides),
    }


def format_duration(seconds):
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def ride_rows(rides):
    rows = []
    for r in rides[:20]:
        dur = format_duration(r["moving_time"])
        rows.append(
            f"<tr>"
            f'<td class="date">{r["date"]}</td>'
            f"<td>{r['name']}</td>"
            f'<td class="type">{r["type"]}</td>'
            f'<td class="num">{r["km"]} km</td>'
            f'<td class="num">{dur}</td>'
            f'<td class="num">{r["elevation"]} m</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def render(stats, rides):
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Bike KM Tracker</title>
  <link rel="stylesheet" href="/style.css" />
</head>
<body>
  <header>
    <h1>🚴 Bike KM Tracker</h1>
    <span class="built-at">Updated {built_at}</span>
  </header>

  <section class="stats-grid">
    <div class="stat-card accent">
      <span class="stat-label">Total km</span>
      <span class="stat-value">{stats['total']}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">This year</span>
      <span class="stat-value">{stats['this_year']}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">This month</span>
      <span class="stat-value">{stats['this_month']}</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">This week</span>
      <span class="stat-value">{stats['this_week']}</span>
    </div>
    <div class="stat-card muted">
      <span class="stat-label">Total rides</span>
      <span class="stat-value">{stats['ride_count']}</span>
    </div>
  </section>

  <section class="rides-section">
    <h2>Recent rides</h2>
    <table>
      <thead>
        <tr>
          <th>Date</th><th>Name</th><th>Type</th>
          <th>Distance</th><th>Duration</th><th>Elevation</th>
        </tr>
      </thead>
      <tbody>
        {ride_rows(rides)}
      </tbody>
    </table>
  </section>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    required = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print("Fetching Strava access token...")
    token = get_access_token()

    print("Fetching rides...")
    rides = fetch_all_rides(token)
    print(f"  Found {len(rides)} rides.")

    stats = compute_stats(rides)
    html = render(stats, rides)

    out_dir = Path("public")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    # Copy static CSS so public/ is self-contained
    css_src = Path(__file__).parent / "static" / "style.css"
    if css_src.exists():
        import shutil
        shutil.copy(css_src, out_dir / "style.css")

    print(f"Built public/index.html  ({stats['total']} km total)")

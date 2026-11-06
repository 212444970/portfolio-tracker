import os
import time
from datetime import datetime, timezone, timedelta

import requests

import token_store

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

CATEGORY_MAP = {
    "Ride": "road",
    "GravelRide": "road",
    "MountainBikeRide": "mtb",
    "VirtualRide": "virtual",
    "EBikeRide": "ebike",
    "AlpineSki": "ski",
    "NordicSki": "ski",
    "BackcountrySki": "ski",
    "Snowboard": "ski",
    "Run": "run",
    "TrailRun": "run",
    "Hike": "hike",
    "Walk": "hike",
    "Swim": "swim",
}

CATEGORY_LABELS = {
    "road": "Silniční kolo",
    "mtb": "Horské kolo",
    "virtual": "Virtuální",
    "ebike": "E-kolo",
    "ski": "Lyže",
    "run": "Běh",
    "hike": "Turistika",
    "swim": "Plavání",
    "other": "Ostatní",
}

_cache = {"activities": None, "fetched_at": 0}
CACHE_TTL = 300


def _refresh_token_if_needed(tokens):
    if time.time() < tokens["expires_at"] - 300:
        return tokens
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    resp.raise_for_status()
    new_tokens = resp.json()
    token_store.save({
        "access_token": new_tokens["access_token"],
        "refresh_token": new_tokens["refresh_token"],
        "expires_at": new_tokens["expires_at"],
    })
    return new_tokens


def exchange_code(code):
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    data = resp.json()
    token_store.save({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    })


def _fetch_all_activities(access_token):
    activities = []
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
            sport = a.get("sport_type") or a.get("type", "")
            km = round(a.get("distance", 0) / 1000, 2)
            elev = round(a.get("total_elevation_gain", 0))
            if km == 0 and elev == 0:
                continue
            cat = CATEGORY_MAP.get(sport, "other")
            activities.append({
                "name": a["name"],
                "date": a["start_date_local"][:10],
                "km": km,
                "elevation": elev,
                "moving_time": a.get("moving_time", 0),
                "type": sport,
                "category": cat,
            })
        if len(batch) < 200:
            break
        page += 1
    return sorted(activities, key=lambda x: x["date"], reverse=True)


def get_activities(force=False):
    global _cache
    if not force and _cache["activities"] is not None:
        if time.time() - _cache["fetched_at"] < CACHE_TTL:
            return _cache["activities"]

    tokens = token_store.load()
    if not tokens:
        return None
    tokens = _refresh_token_if_needed(tokens)
    activities = _fetch_all_activities(tokens["access_token"])
    _cache = {"activities": activities, "fetched_at": time.time()}
    return activities


def get_rides(force=False):
    """Backward-compatible alias."""
    return get_activities(force=force)


def clear_cache():
    global _cache
    _cache = {"activities": None, "fetched_at": 0}


def compute_stats(activities):
    """Basic stats for the JSON API endpoint."""
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def parse(d):
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)

    return {
        "total_km": round(sum(r["km"] for r in activities), 1),
        "this_week_km": round(sum(r["km"] for r in activities if parse(r["date"]) >= week_start), 1),
        "this_month_km": round(sum(r["km"] for r in activities if parse(r["date"]) >= month_start), 1),
        "this_year_km": round(sum(r["km"] for r in activities if parse(r["date"]) >= year_start), 1),
        "activity_count": len(activities),
    }


def format_duration(seconds):
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"

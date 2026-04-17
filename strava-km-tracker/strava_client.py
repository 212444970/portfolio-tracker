import os
import time
from datetime import datetime, timezone, timedelta

import requests

import token_store

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"

# Activity types counted as cycling
RIDE_TYPES = {"Ride", "VirtualRide", "EBikeRide", "GravelRide", "MountainBikeRide"}

_cache = {"rides": None, "fetched_at": 0}
CACHE_TTL = 300  # seconds


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


def _fetch_all_rides(access_token):
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


def get_rides(force=False):
    global _cache
    if not force and _cache["rides"] is not None:
        if time.time() - _cache["fetched_at"] < CACHE_TTL:
            return _cache["rides"]

    tokens = token_store.load()
    if not tokens:
        return None
    tokens = _refresh_token_if_needed(tokens)
    rides = _fetch_all_rides(tokens["access_token"])
    _cache = {"rides": rides, "fetched_at": time.time()}
    return rides


def clear_cache():
    global _cache
    _cache = {"rides": None, "fetched_at": 0}


def compute_stats(rides):
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def parse(d):
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)

    total = sum(r["km"] for r in rides)
    this_week = sum(r["km"] for r in rides if parse(r["date"]) >= week_start)
    this_month = sum(r["km"] for r in rides if parse(r["date"]) >= month_start)
    this_year = sum(r["km"] for r in rides if parse(r["date"]) >= year_start)

    return {
        "total": round(total, 1),
        "this_week": round(this_week, 1),
        "this_month": round(this_month, 1),
        "this_year": round(this_year, 1),
        "ride_count": len(rides),
    }


def format_duration(seconds):
    h, m = divmod(seconds // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"

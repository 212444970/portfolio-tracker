"""
Export Garmin activities using existing Firefox browser session.
No login needed — uses cookies from your already-logged-in Firefox.

Usage:
    python garmin_browser_export.py

If cookies or CSRF token fail automatically, paste them from Firefox DevTools:
  1. Open Firefox, go to connect.garmin.com/activities
  2. Press F12 -> Network tab -> XHR filter
  3. Reload the page (Ctrl+R)
  4. Click the request "activities?limit=20&start=0"
  5. In the "Požadavek" (Request) tab:
       - Copy the Cookie header value -> paste into MANUAL_COOKIE_STRING below
       - Copy the Connect-Csrf-Token value -> paste into MANUAL_CSRF_TOKEN below

Then commit:
    git add garmin_archive.json
    git commit -m "Sync Garmin archive"
    git push
"""
import json
import os
import sys

try:
    from curl_cffi import requests
    _IMPERSONATE = "chrome131"
    print(f"Pouzivam curl_cffi (impersonate={_IMPERSONATE}).")
except ImportError:
    import requests
    _IMPERSONATE = None
    print("curl_cffi neni nainstalovano — pouzivam requests (muze selhat na 401).")

CUTOFF = "2026-08-16"
OUTPUT = "garmin_archive.json"

# -----------------------------------------------------------------------
# MANUAL OVERRIDE — paste from Firefox DevTools if automatic reading fails
# Leave empty ("") to use automatic extraction
# -----------------------------------------------------------------------
MANUAL_COOKIE_STRING = ""
MANUAL_CSRF_TOKEN = ""

CATEGORY_MAP = {
    "cycling": "road",
    "road_biking": "road",
    "gravel_cycling": "road",
    "mountain_biking": "mtb",
    "indoor_cycling": "virtual",
    "virtual_ride": "virtual",
    "alpine_skiing": "ski",
    "backcountry_skiing": "ski",
    "cross_country_skiing": "ski",
    "running": "run",
    "trail_running": "run",
    "hiking": "hike",
    "walking": "hike",
    "swimming": "swim",
    "open_water_swimming": "swim",
}


def parse_cookie_string(s):
    cookies = {}
    for part in s.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            v.encode("latin-1")
            cookies[k] = v
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return cookies


def get_garmin_cookies():
    """Load Garmin cookies from Chrome (via browser-cookie3) + MANUAL_COOKIE_STRING override."""
    cookies = {}

    try:
        import browser_cookie3
        jar = browser_cookie3.chrome(domain_name=".garmin.com")
        cookies = {c.name: c.value for c in jar}
        if cookies:
            print(f"Nacteno {len(cookies)} Garmin cookies z Chrome.")
        else:
            print("Chrome: zadne Garmin cookies — prihlaste se na connect.garmin.com v Chrome.")
    except Exception as e:
        print(f"Chrome nacitani selhalo: {e}")
        print("Nainstalujte: pip install browser-cookie3")

    # MANUAL_COOKIE_STRING doplnuje Chrome cookies (session-only cookies jako SESSIONID)
    if MANUAL_COOKIE_STRING.strip():
        extra = parse_cookie_string(MANUAL_COOKIE_STRING)
        cookies.update(extra)
        print(f"Manual override pridan: {list(extra.keys())}")

    if not cookies:
        print("CHYBA: Zadne Garmin cookies. Prihlaste se v Chrome a zkuste znovu.")
        sys.exit(1)

    return cookies


def get_csrf_token():
    """Returns MANUAL_CSRF_TOKEN (must be set manually from Chrome DevTools)."""
    return MANUAL_CSRF_TOKEN.strip() or None


def test_auth(cookies_dict):
    """Quick auth test; returns (display_name) or exits."""
    kwargs = dict(
        headers={"NK": "NT", "User-Agent": "Mozilla/5.0"},
        cookies=cookies_dict,
    )
    if _IMPERSONATE:
        kwargs["impersonate"] = _IMPERSONATE
    resp = requests.get(
        "https://connect.garmin.com/modern/currentuser-service/user/info",
        **kwargs,
    )
    print(f"Auth test -> HTTP {resp.status_code}")
    try:
        info = resp.json()
        display_name = info.get("username") or info.get("displayName") or ""
        print(f"  Uzivatel: {display_name}")
        return display_name
    except Exception:
        print(f"  Odpoved: {resp.text[:200]}")
        return ""


def _try_fetch_page(url, params, headers, cookies_dict):
    """Fetch one page; return (batch_list, raw_text, status_code)."""
    try:
        kwargs = dict(params=params, headers=headers, cookies=cookies_dict, timeout=20)
        if _IMPERSONATE:
            kwargs["impersonate"] = _IMPERSONATE
        resp = requests.get(url, **kwargs)
    except Exception as e:
        return None, str(e), 0
    status = resp.status_code
    if status not in (200, 404):
        return None, resp.text[:200], status
    try:
        data = resp.json()
    except Exception:
        return None, resp.text[:200], status
    if isinstance(data, list):
        return data, resp.text, status
    if isinstance(data, dict):
        batch = data.get("activityList") or data.get("activities") or data.get("data") or []
        if not batch and data:
            keys = list(data.keys())
            return [], f"dict keys: {keys}", status
        return batch, resp.text, status
    return [], resp.text, status


def fetch_activities(cookies_dict):
    display_name = test_auth(cookies_dict)
    csrf_token = get_csrf_token()
    print(f"  CSRF token: {'nalezen (' + csrf_token[:12] + '...)' if csrf_token else 'NENALEZEN — zkousim bez nej'}")

    # Diagnostika — klic auth cookies
    for key in ["JWT_WEB", "SESSIONID", "GARMIN-SSO-CUST-GUID", "session", "__cflb"]:
        val = cookies_dict.get(key, "")
        print(f"  Cookie {key}: {'OK (' + val[:16] + '...)' if val else 'CHYBI'}")

    jwt_web = cookies_dict.get("JWT_WEB", "")

    def make_headers(with_csrf=True):
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "cs,sk;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if with_csrf and csrf_token:
            h["Connect-Csrf-Token"] = csrf_token
        return h

    # Garmin Connect React app (v5.27+) uses /gc-api/ prefix
    candidate_urls = [
        "https://connect.garmin.com/gc-api/activitylist-service/activities/search/activities",
        "https://connect.garmin.com/proxy/activitylist-service/activities/search/activities",
        "https://connect.garmin.com/api/proxy/activitylist-service/activities/search/activities",
    ]

    base_params = {"start": 0, "limit": 1}

    strategies = []
    for cu in candidate_urls:
        for hdr_label, hdr in [("csrf", make_headers(True)), ("no-csrf", make_headers(False))]:
            strategies.append((cu, base_params, hdr, f"{cu.split('garmin.com')[1][:35]} [{hdr_label}]"))

    url = candidate_urls[0]
    headers = make_headers()
    params = base_params
    for url, params, hdr, label in strategies:
        print(f"  Zkousim {label}")
        try:
            kw = dict(params=params, headers=hdr, cookies=cookies_dict, timeout=20)
            if _IMPERSONATE:
                kw["impersonate"] = _IMPERSONATE
            resp = requests.get(url, **kw)
            ct = resp.headers.get("Content-Type", "?")
            print(f"  -> HTTP {resp.status_code} | Content-Type: {ct[:60]}")
            if resp.status_code not in (200, 404):
                print(f"     Telo: {resp.text[:120]}")
                continue
            try:
                data = resp.json()
            except Exception:
                print(f"     Neni JSON — zrejme HTML nebo redirect. Telo: {resp.text[:120]}")
                continue
            if isinstance(data, list) and data:
                batch = data
            elif isinstance(data, dict):
                batch = data.get("activityList") or data.get("activities") or data.get("data") or []
                if not batch:
                    print(f"     JSON dict, klice: {list(data.keys())[:10]}")
                    continue
            else:
                print(f"     Prazdna odpoved: {data}")
                continue
            print(f"  Funguje! Pouzivam.")
            headers = hdr
            break
        except Exception as e:
            print(f"  -> Chyba: {e}")
    else:
        print("\nCHYBA: Zadna strategie nefunguje.")
        if not csrf_token:
            print("Nejspis chybi CSRF token. Spus znovu s MANUAL_CSRF_TOKEN:")
            print("  1. Firefox -> connect.garmin.com/activities")
            print("  2. F12 -> Network -> XHR -> nacti stranku")
            print('  3. Klikni na "activities?limit=20&start=0"')
            print("  4. V Request headers zkopiruj hodnotu Connect-Csrf-Token")
            print("  5. Vloz ji do MANUAL_CSRF_TOKEN na zacatku skriptu")
        sys.exit(1)

    # Fetch all pages with the working url/headers
    winning_extra = {k: v for k, v in params.items() if k not in ("start", "limit")}

    all_raw = []
    start = 0
    limit = 20  # match what the browser uses

    while True:
        print(f"  Stahuji aktivity {start}-{start + limit}...")
        page_params = {"start": start, "limit": limit, **winning_extra}
        batch, raw, status = _try_fetch_page(url, page_params, headers, cookies_dict)
        if not batch:
            print(f"  Konec: {str(raw)[:80]}")
            break
        all_raw.extend(batch)
        oldest = (batch[-1].get("startTimeLocal") or "")[:10]
        if oldest and oldest < CUTOFF:
            break
        if len(batch) < limit:
            break
        start += limit

    return all_raw


def map_activities(all_raw):
    activities = []
    for a in all_raw:
        sport = (a.get("activityType") or {}).get("typeKey", "other")
        km = round((a.get("distance") or 0) / 1000, 2)
        elev = round(a.get("elevationGain") or 0)
        if km == 0 and elev == 0:
            continue
        start_local = a.get("startTimeLocal") or ""
        date_str = start_local[:10]
        if not date_str or date_str < CUTOFF:
            continue
        time_str = start_local[11:16] if len(start_local) > 10 else ""
        moving = int(a.get("movingDuration") or a.get("duration") or 0)
        activities.append({
            "name": a.get("activityName") or sport,
            "date": date_str,
            "time": time_str,
            "km": km,
            "elevation": elev,
            "moving_time": moving,
            "type": sport,
            "category": CATEGORY_MAP.get(sport, "other"),
            "source": "garmin",
        })
    activities.sort(key=lambda x: x["date"], reverse=True)
    return activities


def main():
    cookies_dict = get_garmin_cookies()
    print("Stahuji aktivity z Garmin Connect...")
    all_raw = fetch_activities(cookies_dict)
    print(f"Stazeno {len(all_raw)} zaznamu.")
    activities = map_activities(all_raw)
    print(f"Zpracovano {len(activities)} aktivit (bez nulovych).")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)

    print(f"\nHotovo! Ulozeno do {OUTPUT}")
    print("\nDalsi krok:")
    print(f"  git add {OUTPUT}")
    print(f'  git commit -m "Sync Garmin archive"')
    print("  git push")


if __name__ == "__main__":
    main()

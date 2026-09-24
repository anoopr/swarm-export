#!/usr/bin/env python3
"""Export your Swarm (Foursquare) check-ins, photos, and categories.

Writes month files (YYYY/MM/YYYY-MM-checkins.json) with full-resolution photos
alongside, the Foursquare category taxonomy and icons (categories/), and an
offline HTML viewer (index.html). See README.md for setup.

Re-runs are incremental: only check-ins from the last 30 days before your newest
exported one are re-fetched. Use --full to re-fetch everything.

Credentials come from environment variables or a .env file next to this script
(copy .env.example to start one):

    FSQ_TOKEN=...  ./swarm_export.py

    # No token yet? Supply your app's client ID/secret and the script runs the
    # OAuth flow in your browser first, then prints the token for reuse.
    FSQ_CLIENT_ID=... FSQ_CLIENT_SECRET=...  ./swarm_export.py
"""
from __future__ import annotations

import argparse
import glob
import http.client
import http.server
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.foursquare.com/v2/"
AUTHORIZE_URL = "https://foursquare.com/oauth2/authenticate"
TOKEN_URL = "https://foursquare.com/oauth2/access_token"
# Foursquare's `v` param is a YYYYMMDD date that pins the response format.
API_VERSION = "20260223"
PAGE_SIZE = 250  # max the endpoint allows
# Incremental runs re-fetch this far back from the newest exported check-in, to
# pick up recent edits, late photos, and deletions.
OVERLAP_DAYS = 30
MAX_RETRIES = 5
DOWNLOAD_WORKERS = 8
ICON_SIZE = "512"  # largest size the icon CDN serves
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_TEMPLATE = os.path.join(SCRIPT_DIR, "viewer", "index.html")


class ExportError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_dotenv(path: str) -> None:
    """Set KEY=VALUE pairs from a .env file, without overriding the real environment."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        key, sep, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key, value.strip().strip("'\""))


def error_detail(err: urllib.error.HTTPError) -> str:
    try:
        meta = json.load(err)["meta"]
        return f"{meta.get('errorType', 'error')}: {meta.get('errorDetail', '')}"
    except (ValueError, KeyError, TypeError):
        return str(err.reason)


def retry_delay(headers, attempt: int) -> float:
    if headers.get("Retry-After", "").isdigit():
        return float(headers["Retry-After"])
    reset = headers.get("X-RateLimit-Reset", "")
    if reset.isdigit():
        return max(float(reset) - time.time(), 1.0)
    return float(2**attempt)


def fetch_bytes(url: str) -> bytes:
    """GET a URL, retrying rate limits, server errors, and network errors."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = error_detail(e)
            rate_limited = e.code == 429 or "rate_limit" in detail
            if not (rate_limited or e.code >= 500) or attempt == MAX_RETRIES:
                raise ExportError(f"Request failed (HTTP {e.code}) {detail}") from None
            delay = retry_delay(e.headers, attempt)
            log(f"  HTTP {e.code} ({detail}); retrying in {delay:.0f}s...")
        except (OSError, http.client.HTTPException) as e:
            if attempt == MAX_RETRIES:
                raise ExportError(f"Network error: {e}") from None
            delay = float(2**attempt)
            log(f"  network error ({e}); retrying in {delay:.0f}s...")
        time.sleep(delay)
    raise AssertionError("unreachable")


def api_get(endpoint: str, params: dict, token: str) -> dict:
    query = urllib.parse.urlencode({**params, "oauth_token": token, "v": API_VERSION})
    return json.loads(fetch_bytes(f"{API_BASE}{endpoint}?{query}"))["response"]


def utc_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_checkins(token: str, since: int | None = None) -> list[dict]:
    """Fetch check-ins newest first, back to `since` (unix seconds) or the very first one."""
    # Page backwards with beforeTimestamp: Foursquare stops honoring `offset`
    # after a few hundred results and silently repeats the first page.
    checkins: list[dict] = []
    seen: set[str] = set()
    before = None
    while True:
        params = {"limit": PAGE_SIZE, "sort": "newestfirst"}
        if before is not None:
            params["beforeTimestamp"] = before
        page = api_get("users/self/checkins", params, token)["checkins"]
        new = [c for c in page["items"] if c["id"] not in seen]
        if not new:
            break
        seen.update(c["id"] for c in new)
        checkins.extend(new)
        oldest = min(c["createdAt"] for c in new)
        log(f"Fetched {len(checkins)} of {page.get('count', '?')} check-ins (back to {utc_date(oldest)})")
        if since is not None and oldest < since:
            break
        # +1 so check-ins sharing the oldest second aren't skipped; repeats
        # are filtered by id above.
        before = oldest + 1
    if not checkins:
        # Guards against wiping the export if the API misbehaves.
        raise ExportError("The API returned no check-ins; leaving the export untouched.")
    return checkins


def month_files(out_dir: str) -> list[str]:
    return glob.glob(os.path.join(out_dir, "[0-9]" * 4, "[0-9]" * 2, "*-checkins.json"))


def load_existing(out_dir: str) -> list[dict]:
    """Check-ins from a previous export's month files."""
    checkins = []
    for path in month_files(out_dir):
        try:
            with open(path, encoding="utf-8") as f:
                checkins.extend(json.load(f))
        except ValueError as e:
            raise ExportError(f"Couldn't read {path} ({e}); run with --full to rebuild the export.") from None
    return checkins


def merge_checkins(existing: list[dict], fetched: list[dict], since: int) -> list[dict]:
    """Combine a previous export with freshly fetched check-ins. Fetched copies win,
    and check-ins from `since` onward that the API no longer returns were deleted."""
    fetched_ids = {c["id"] for c in fetched}
    old = {c["id"]: c for c in existing}
    kept = [c for c in existing if c["id"] not in fetched_ids and c["createdAt"] < since]
    added = sum(c["id"] not in old for c in fetched)
    changed = sum(c["id"] in old and old[c["id"]] != c for c in fetched)
    deleted = sum(c["id"] not in fetched_ids and c["createdAt"] >= since for c in existing)
    log(f"{added} new, {changed} changed, {deleted} deleted check-ins")
    return sorted(fetched + kept, key=lambda c: c["createdAt"], reverse=True)


def fetch_categories(token: str) -> list[dict]:
    categories = api_get("venues/categories", {}, token)["categories"]
    log(f"Fetched category taxonomy ({len(categories)} top-level categories)")
    return categories


def flatten_categories(taxonomy: list[dict], checkins: list[dict]) -> dict[str, tuple[dict, str | None]]:
    """Map category id -> (category, parent id) across the whole taxonomy, plus
    any visited category the taxonomy doesn't list."""
    flat: dict[str, tuple[dict, str | None]] = {}
    stack: list[tuple[dict, str | None]] = [(node, None) for node in taxonomy]
    while stack:
        node, parent = stack.pop()
        flat[node["id"]] = (node, parent)
        stack.extend((child, node["id"]) for child in node.get("categories", []))
    for c in checkins:
        for cat in c.get("venue", {}).get("categories", []):
            flat.setdefault(cat["id"], (cat, None))
    return flat


def local_time(checkin: dict) -> datetime:
    """The check-in's wall-clock time where it happened."""
    tz = timezone(timedelta(minutes=checkin.get("timeZoneOffset", 0)))
    return datetime.fromtimestamp(checkin["createdAt"], tz)


def photo_path(checkin: dict, photo: dict) -> str:
    """Photo location relative to the export dir, e.g. 2024/03/2024-03-15_<id>.jpg."""
    t = local_time(checkin)
    ext = os.path.splitext(photo["suffix"])[1] or ".jpg"
    return f"{t:%Y/%m}/{t:%Y-%m-%d}_{photo['id']}{ext}"


def icon_path(icon: dict) -> str:
    """Icon location relative to the export dir, e.g. categories/icons/food_winery.png."""
    # Icon prefixes look like https://ss3.4sqi.net/img/categories_v2/food/winery_
    path = urllib.parse.urlparse(icon["prefix"]).path
    name = path.split("categories_v2/")[-1].strip("/_").replace("/", "_")
    return f"categories/icons/{name}{icon['suffix']}"


def write_json(path: str, data) -> bool:
    """Write data as JSON unless the file already holds exactly that; return whether it was written."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def write_month_files(checkins: list[dict], out_dir: str) -> None:
    months: dict[str, list[dict]] = defaultdict(list)
    for c in checkins:
        months[local_time(c).strftime("%Y/%m")].append(c)
    paths = {
        os.path.normpath(os.path.join(out_dir, month, f"{month.replace('/', '-')}-checkins.json")): items
        for month, items in months.items()
    }
    written = sum(write_json(path, items) for path, items in paths.items())
    # Months whose check-ins were all deleted lose their file (photos are kept).
    stale = [path for path in map(os.path.normpath, month_files(out_dir)) if path not in paths]
    for path in stale:
        os.remove(path)
    log(
        f"{len(checkins)} check-ins across {len(months)} months; updated {written} month files"
        + (f", removed {len(stale)}" if stale else "")
    )


def download_all(jobs: dict[str, str], label: str) -> list[str]:
    """Download {dest: url} pairs not already on disk; return the dests that failed."""
    todo = {dest: url for dest, url in jobs.items() if not os.path.exists(dest)}
    log(f"{label}: {len(jobs) - len(todo)} already downloaded, {len(todo)} to fetch")

    def download(dest: str, url: str) -> None:
        data = fetch_bytes(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Write then rename, so an interrupted run never leaves a partial file
        # that the next run would mistake for a finished download.
        with open(dest + ".part", "wb") as f:
            f.write(data)
        os.replace(dest + ".part", dest)

    failed = []
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(download, dest, url): dest for dest, url in todo.items()}
        for done, future in enumerate(as_completed(futures), 1):
            try:
                future.result()
            except (ExportError, OSError) as e:
                failed.append(futures[future])
                log(f"  failed {futures[future]}: {e}")
            if done % 100 == 0 or done == len(todo):
                log(f"  {label}: {done}/{len(todo)}")
    return failed


def viewer_checkin(c: dict, out_dir: str) -> dict:
    """The slice of a check-in the viewer needs."""
    venue = None
    if "venue" in c:
        v = c["venue"]
        loc = v.get("location", {})
        cats = v.get("categories", [])
        primary = next((cat for cat in cats if cat.get("primary")), cats[0] if cats else None)
        venue = {
            "name": v.get("name"),
            "categoryId": primary["id"] if primary else None,
            "address": loc.get("address"),
            "city": loc.get("city"),
            "state": loc.get("state"),
            "country": loc.get("country"),
            "lat": loc.get("lat"),
            "lng": loc.get("lng"),
        }
    photos = []
    for p in c.get("photos", {}).get("items", []):
        src = photo_path(c, p)
        if os.path.exists(os.path.join(out_dir, src)):
            photos.append({"src": src, "w": p.get("width"), "h": p.get("height")})
    return {
        "id": c["id"],
        "createdAt": c["createdAt"],
        "tz": c.get("timeZoneOffset", 0),
        "url": c.get("canonicalUrl"),
        "shout": c.get("shout"),
        "private": c.get("private", False),
        "venue": venue,
        "photos": photos,
        "with": [
            " ".join(filter(None, [u.get("firstName"), u.get("lastName")])) for u in c.get("with", [])
        ],
        "event": c.get("event", {}).get("name"),
    }


def category_icon(cid: str | None, categories: dict[str, tuple[dict, str | None]], out_dir: str) -> str | None:
    """A category's downloaded icon, falling back to its nearest ancestor's (a few
    icons don't exist on Foursquare's CDN)."""
    while cid in categories:
        cat, parent = categories[cid]
        if "icon" in cat and os.path.exists(os.path.join(out_dir, icon_path(cat["icon"]))):
            return icon_path(cat["icon"])
        cid = parent
    return None


def write_viewer(checkins: list[dict], categories: dict[str, tuple[dict, str | None]], out_dir: str) -> None:
    data = {
        "generatedAt": int(time.time()),
        "categories": {
            cid: {"name": cat["name"], "icon": category_icon(cid, categories, out_dir), "parent": parent}
            for cid, (cat, parent) in categories.items()
        },
        "checkins": [viewer_checkin(c, out_dir) for c in checkins],
    }
    # A script file (not JSON) so index.html can load it straight from disk.
    with open(os.path.join(out_dir, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.SWARM_DATA = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    shutil.copyfile(VIEWER_TEMPLATE, os.path.join(out_dir, "index.html"))
    log(f"Wrote viewer to {os.path.join(out_dir, 'index.html')}")


def authorize(client_id: str, client_secret: str) -> str:
    """Run the OAuth code flow via a one-shot local server; return an access token."""
    result: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":
                self.send_error(404)
                return
            result.update(urllib.parse.parse_qsl(url.query))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorized. You can close this tab and return to the terminal.")

        def log_message(self, *args):
            pass

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI}
    )
    with http.server.HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler) as server:
        log(f"Opening your browser to authorize. If it doesn't open, visit:\n  {auth_url}")
        webbrowser.open(auth_url)
        while "code" not in result and "error" not in result:
            server.handle_request()
    if "error" in result:
        raise ExportError(f"Authorization failed: {result['error']}")

    query = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
        "code": result["code"],
    })
    try:
        with urllib.request.urlopen(f"{TOKEN_URL}?{query}", timeout=60) as resp:
            return json.load(resp)["access_token"]
    except urllib.error.HTTPError as e:
        raise ExportError(f"Token exchange failed (HTTP {e.code}) {error_detail(e)}") from None


def get_token() -> str:
    token = os.environ.get("FSQ_TOKEN")
    if token:
        return token
    client_id = os.environ.get("FSQ_CLIENT_ID")
    client_secret = os.environ.get("FSQ_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise ExportError(
            "Set FSQ_TOKEN, or FSQ_CLIENT_ID and FSQ_CLIENT_SECRET to log in: copy .env.example "
            "to .env and fill it in. See README.md."
        )
    token = authorize(client_id, client_secret)
    log(f"Got access token. To skip login next time, set it in .env:\n  FSQ_TOKEN={token}")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Export your Swarm check-ins, photos, and categories.")
    parser.add_argument("-o", "--out-dir", default="export", help="output directory (default: export)")
    parser.add_argument(
        "--full", action="store_true",
        help=f"re-fetch every check-in and the category taxonomy, not just the last {OVERLAP_DAYS} days",
    )
    parser.add_argument("--no-photos", action="store_true", help="skip downloading check-in photos")
    args = parser.parse_args()
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    out = args.out_dir
    taxonomy_path = os.path.join(out, "categories", "categories.json")

    try:
        token = get_token()
        existing = [] if args.full else load_existing(out)
        if existing:
            since = max(c["createdAt"] for c in existing) - OVERLAP_DAYS * 86400
            log(f"Updating {len(existing)} exported check-ins; re-checking from {utc_date(since)} on")
            checkins = merge_checkins(existing, fetch_checkins(token, since), since)
        else:
            checkins = fetch_checkins(token)
        if args.full or not os.path.exists(taxonomy_path):
            write_json(taxonomy_path, fetch_categories(token))
    except ExportError as e:
        log(f"Error: {e}")
        return 1

    write_month_files(checkins, out)
    with open(taxonomy_path, encoding="utf-8") as f:
        categories = flatten_categories(json.load(f), checkins)

    icons = {
        os.path.join(out, icon_path(cat["icon"])): cat["icon"]["prefix"] + ICON_SIZE + cat["icon"]["suffix"]
        for cat, _ in categories.values()
        if "icon" in cat
    }
    if download_all(icons, "Category icons"):
        log("  (Categories without an icon show their parent category's icon in the viewer.)")
    failed = []
    if not args.no_photos:
        photos = {
            os.path.join(out, photo_path(c, p)): p["prefix"] + "original" + p["suffix"]
            for c in checkins
            for p in c.get("photos", {}).get("items", [])
        }
        failed = download_all(photos, "Photos")

    write_viewer(checkins, categories, out)
    if failed:
        log(f"{len(failed)} photo downloads failed; run again to retry them.")
        return 1
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

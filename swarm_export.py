#!/usr/bin/env python3
"""Export your Swarm (Foursquare) check-in history to a JSON file.

Credentials come from environment variables (see README.md for setup):

    FSQ_TOKEN=...  ./swarm_export.py -o checkins.json

    # No token yet? Supply your app's client ID/secret and the script runs the
    # OAuth flow in your browser first, then prints the token for reuse.
    FSQ_CLIENT_ID=... FSQ_CLIENT_SECRET=...  ./swarm_export.py
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

API_URL = "https://api.foursquare.com/v2/users/self/checkins"
AUTHORIZE_URL = "https://foursquare.com/oauth2/authenticate"
TOKEN_URL = "https://foursquare.com/oauth2/access_token"
# Foursquare's `v` param is a YYYYMMDD date that pins the response format.
API_VERSION = "20260223"
PAGE_SIZE = 250  # max the endpoint allows
MAX_RETRIES = 5
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


class ExportError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


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


def api_get(params: dict, token: str) -> dict:
    query = urllib.parse.urlencode({**params, "oauth_token": token, "v": API_VERSION})
    url = f"{API_URL}?{query}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)["response"]
        except urllib.error.HTTPError as e:
            detail = error_detail(e)
            rate_limited = e.code == 429 or "rate_limit" in detail
            if not (rate_limited or e.code >= 500) or attempt == MAX_RETRIES:
                raise ExportError(f"API request failed (HTTP {e.code}) {detail}") from None
            delay = retry_delay(e.headers, attempt)
            log(f"  HTTP {e.code} ({detail}); retrying in {delay:.0f}s...")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == MAX_RETRIES:
                raise ExportError(f"Network error: {e}") from None
            delay = float(2**attempt)
            log(f"  network error ({e}); retrying in {delay:.0f}s...")
        time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_all_checkins(token: str) -> list[dict]:
    # Page backwards with beforeTimestamp: Foursquare stops honoring `offset`
    # after a few hundred results and silently repeats the first page.
    checkins: list[dict] = []
    seen: set[str] = set()
    before = None
    while True:
        params = {"limit": PAGE_SIZE, "sort": "newestfirst"}
        if before is not None:
            params["beforeTimestamp"] = before
        page = api_get(params, token)["checkins"]
        new = [c for c in page["items"] if c["id"] not in seen]
        if not new:
            break
        seen.update(c["id"] for c in new)
        checkins.extend(new)
        oldest = min(c["createdAt"] for c in new)
        # +1 so check-ins sharing the oldest second aren't skipped; repeats
        # are filtered by id above.
        before = oldest + 1
        date = datetime.fromtimestamp(oldest, tz=timezone.utc).strftime("%Y-%m-%d")
        log(f"Fetched {len(checkins)} of {page.get('count', '?')} check-ins (back to {date})")
    return checkins


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
            "Set FSQ_TOKEN, or FSQ_CLIENT_ID and FSQ_CLIENT_SECRET to log in. See README.md."
        )
    token = authorize(client_id, client_secret)
    log(f"Got access token. To skip login next time:\n  export FSQ_TOKEN={token}")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Export your Swarm check-ins to JSON.")
    parser.add_argument(
        "-o", "--output", default="checkins.json",
        help="output file, or '-' for stdout (default: checkins.json)",
    )
    args = parser.parse_args()

    try:
        checkins = fetch_all_checkins(get_token())
    except ExportError as e:
        log(f"Error: {e}")
        return 1

    if args.output == "-":
        json.dump(checkins, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(checkins, f, ensure_ascii=False, indent=2)
            f.write("\n")
    log(f"Wrote {len(checkins)} check-ins to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

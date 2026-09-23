# swarm-export

Exports your full Swarm / Foursquare check-in history to a JSON file. It's a single Python 3 script that uses only the standard library.

The output is a JSON array of check-in objects exactly as the Foursquare v2 API returns them (newest first). Each includes `createdAt` (unix seconds), `timeZoneOffset`, the `venue` with location and categories, `shout`, and photo/like/comment summaries.

## Setup (one time)

1. Sign in at <https://foursquare.com/developers/> and create an app (any name works).
2. In the app's settings, set **Redirect URL** to `http://localhost:8765/callback`.
3. Note the app's **Client ID** and **Client Secret**.

## Usage

On the first run, provide the client credentials. The script opens your browser so you can approve access, then prints an access token:

```bash
FSQ_CLIENT_ID=your_id FSQ_CLIENT_SECRET=your_secret python3 swarm_export.py
```

On later runs, reuse the token:

```bash
FSQ_TOKEN=your_token python3 swarm_export.py -o checkins.json
```

`-o -` writes the JSON to stdout.

## Notes

- Pagination uses `beforeTimestamp` rather than `offset`, because Foursquare stops honoring `offset` after a few hundred results.
- Each request fetches 250 check-ins, so even a large history takes only a few dozen API calls. If you hit a rate limit or a server error, the script retries automatically.

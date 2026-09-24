# swarm-export

Exports your full Swarm / Foursquare check-in history, with full-resolution photos and Foursquare's category taxonomy, plus an offline HTML viewer for browsing it. It's a single Python 3 script that uses only the standard library.

## Setup (one time)

1. Sign in at <https://foursquare.com/developers/> and create an app (any name works).
2. In the app's settings, set **Redirect URL** to `http://localhost:8765/callback`.
3. Note the app's **Client ID** and **Client Secret**.

## Usage

On the first run, provide the client credentials. The script opens your browser so you can approve access, then prints an access token:

```bash
FSQ_CLIENT_ID=your_id FSQ_CLIENT_SECRET=your_secret python3 swarm_export.py
```

After that, save the token in a `.env` file next to the script. It's gitignored, and variables already set in your environment take precedence:

```
FSQ_TOKEN=your_token
```

Then just run:

```bash
python3 swarm_export.py
```

| Flag | Default | |
|---|---|---|
| `-o, --out-dir` | `export` | Where to write everything |
| `--no-photos` | off | Skip downloading photos |

Re-running is safe and quick. Check-ins are re-fetched in full, which takes about 55 API calls. Photos and icons already on disk are skipped, so only new ones are downloaded.

## Output

```
export/
  index.html                  # the viewer: double-click to open (works offline)
  data.js                     # data for the viewer
  categories/
    categories.json           # Foursquare's full category tree, as the API returns it
    icons/food_winery.png     # category icons at 512px (white on transparent)
  2024/03/
    2024-03-checkins.json     # that month's check-ins exactly as the API returns them
    2024-03-15_<photoId>.jpg  # full-resolution photos from those check-ins
```

- Check-ins are grouped by the local time where they happened, not UTC.
- A photo's filename date is its check-in's local date.

## Viewer

Open `export/index.html` in any browser. It has:

- a month-by-month **timeline**
- a **Photos** grid, with a full-size view you can page through with the arrow keys
- **filters** that apply to both views: search (venue, shout, place, people, category), a category tree (picking a parent includes its subcategories), country and city, a date range, and "with photos"

## Notes

- Pagination uses `beforeTimestamp` rather than `offset`, because Foursquare stops honoring `offset` after a few hundred results.
- The check-in count Foursquare reports can be slightly higher than what the API actually returns. Paging through the history in different ways always yields the same set.
- A few category icons don't exist on Foursquare's CDN. The viewer shows the parent category's icon for those.
- If a download fails because of a rate limit or a server error, it's retried automatically. Photos that still fail are listed, and the next run picks them up.

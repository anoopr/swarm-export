# swarm-export

Exports your full Swarm / Foursquare check-in history, with full-resolution photos and Foursquare's category taxonomy, plus an offline HTML viewer for browsing it. It's a single Python 3 script that uses only the standard library.

## Setup (one time)

1. Sign in at <https://foursquare.com/developers/> and create an app (any name works).
2. In the app's settings, set **Redirect URL** to `http://localhost:8765/callback`.
3. Copy the example config. `.env` is gitignored, so your credentials stay out of the repo:

   ```bash
   cp .env.example .env
   ```

4. In `.env`, set `FSQ_CLIENT_ID` and `FSQ_CLIENT_SECRET` to your app's **Client ID** and **Client Secret**.
5. Run the script once. With no token yet, it opens your browser so you can approve access, then prints an access token:

   ```bash
   python3 swarm_export.py
   ```

6. Paste that token into `.env` as `FSQ_TOKEN`. Later runs use it directly and skip the login.

Variables already set in your shell take precedence over `.env`.

## Usage

```bash
python3 swarm_export.py
```

| Flag | Default | |
|---|---|---|
| `-o, --out-dir` | `export` | Where to write everything |
| `--full` | off | Re-fetch every check-in and the category taxonomy |
| `--no-photos` | off | Skip downloading photos |

### Re-running

Re-runs are incremental and take a couple of seconds.

- **Check-ins:** the script loads the existing month files and fetches only check-ins from 30 days before your newest exported one onward, usually a single API call. Everything it fetches replaces the copy on disk, so new check-ins, edited shouts, late photos, and deleted check-ins in that window are all picked up.
- **Files:** only month files whose contents changed are rewritten.
- **Categories:** the category list is reused from `categories/categories.json`.
- **Photos and icons:** anything already on disk is skipped.

Edits or deletions to check-ins older than that window are only picked up by `--full`, which re-fetches everything (about 55 API calls).

In either mode, a month whose check-ins have all been deleted loses its JSON file, but its photos are kept. If the API ever returns no check-ins at all, the script stops without touching the export.

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

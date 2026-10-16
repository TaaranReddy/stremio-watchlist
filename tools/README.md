# tools/
- `resolve_netflix2.py` — resolves a Netflix viewing-activity dump to IMDb ids (parallel,
  episode-string aware). Input `netflix-viewing.json`, output `netflix-resolved.json`.
- `build_netflix.py` — clean rebuild: canonical titles -> `netflix-catalog.json`
  (-> repo root `netflix-watched.json`, which the addon serves as the Netflix rows).
- `sync_addon_collection.py` — after a MANIFEST change, push the published manifest into
  Taaran's Stremio account (backs up the collection first, then reads back).
Run with /opt/homebrew/bin/python3 (needs the repo's `watchlist.py` importable).

## mark_watched2.py — put watch history INTO the Stremio account
Marks the Netflix import (netflix-watched.json) as watched in the Stremio
library itself, so every client qualifies it as seen (ticks, sorting,
recommendations) and not just a catalog row. Handles tt/tmdb id twins by
name, writes in batches of 150, and writes an undo script (`restore_library.py`)
plus before/after library snapshots into ~/Desktop/requests/Stremio-Audit/.
`--test` writes three items only.

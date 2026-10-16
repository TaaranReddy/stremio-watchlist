# tools/
- `resolve_netflix2.py` — resolves a Netflix viewing-activity dump to IMDb ids (parallel,
  episode-string aware). Input `netflix-viewing.json`, output `netflix-resolved.json`.
- `build_netflix.py` — clean rebuild: canonical titles -> `netflix-catalog.json`
  (-> repo root `netflix-watched.json`, which the addon serves as the Netflix rows).
- `sync_addon_collection.py` — after a MANIFEST change, push the published manifest into
  Taaran's Stremio account (backs up the collection first, then reads back).
Run with /opt/homebrew/bin/python3 (needs the repo's `watchlist.py` importable).

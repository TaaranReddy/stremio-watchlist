#!/usr/bin/env python3
"""FINAL: mark the Netflix history as watched in Stremio + an undo script.

Rules:
  * tt-id already in the library      -> update that item's state (keep its other fields)
  * same NAME+TYPE already in library -> mark THAT item watched instead of adding a
    duplicate under a different meta id (his library mixes tt: and tmdb: ids)
  * otherwise                         -> add a new item mirroring the exact shape of
    the items already in his library (incl. removed/temp true)

  python3 mark_watched2.py --test   # 3 items, read back
  python3 mark_watched2.py          # full run (writes backups + undo script)
"""
import datetime, json, os, re, sys, time, urllib.request

REPO = '/Users/taaran/Code/whatsapp-stremio-watchlist'
API = 'https://api.strem.io/api/'
BK = os.path.expanduser('~/Desktop/requests/Stremio-Audit')
KEY = json.load(open(os.path.join(REPO, 'config.json')))['stremio_authkey']
TEST = '--test' in sys.argv


def api(method, payload, timeout=180):
    req = urllib.request.Request(API + method, data=json.dumps(payload).encode(),
                                 headers={'content-type': 'application/json',
                                          'user-agent': 'stremio-watched-import'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def day_iso(day):
    return f'{day}T12:00:00.000Z' if day and len(day) == 10 else now_iso()


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def watched_state(tid, typ, last):
    return {
        'lastWatched': day_iso(last),
        'timeWatched': 0, 'timeOffset': 0, 'overallTimeWatched': 0,
        'timesWatched': 1, 'flaggedWatched': 1,
        'video_id': tid if typ == 'movie' else f'{tid}:1:1',
        'noNotif': False, 'season': 0, 'episode': 0,
    }


def main():
    lib = api('datastoreGet', {'authKey': KEY, 'collection': 'libraryItem', 'all': True})['result']
    stamp = time.strftime('%Y%m%d-%H%M%S')
    before = os.path.join(BK, f'library-before-markwatched-{stamp}.json')
    json.dump(lib, open(before, 'w'), indent=1)
    print(f'library before: {len(lib)} items -> {before}')

    have_id = {i['_id']: i for i in lib}
    have_name = {}
    for i in lib:
        have_name.setdefault((norm(i.get('name')), i.get('type')), i)

    items = json.load(open(os.path.join(REPO, 'netflix-watched.json')))
    targets = items[:3] if TEST else items
    changes, added, updated, via_twin = [], 0, 0, []
    for it in targets:
        tid, typ = it['id'], it['type']
        st = watched_state(tid, typ, it.get('last_seen'))
        if tid in have_id:
            cur = dict(have_id[tid]); st = {**(cur.get('state') or {}), **st}
            cur['state'] = st; cur['_mtime'] = now_iso()
            changes.append(cur); updated += 1
        else:
            twin = have_name.get((norm(it.get('name')), typ))
            if twin and not str(twin['_id']).startswith('tt'):
                cur = dict(twin); st = {**(cur.get('state') or {}), **st}
                cur['state'] = st; cur['_mtime'] = now_iso()
                changes.append(cur); updated += 1; via_twin.append(twin['_id'])
            else:
                changes.append({
                    '_id': tid, 'name': it.get('name') or tid, 'type': typ,
                    'poster': f'https://images.metahub.space/poster/small/{tid}/img',
                    'posterShape': 'poster', 'background': '', 'logo': '',
                    'year': it.get('year') or '', 'removed': True, 'temp': True,
                    '_ctime': now_iso(), '_mtime': now_iso(), 'state': st,
                })
                added += 1
    print(f'changes: {len(changes)} (new {added}, updated {updated}'
          + (f", {len(via_twin)} via tmdb-twin: {via_twin}" if via_twin else '') + ')')

    for i in range(0, len(changes), 150):
        chunk = changes[i:i + 150]
        res = api('datastorePut', {'authKey': KEY, 'collection': 'libraryItem', 'changes': chunk})
        print(f'  batch {i // 150 + 1}: {len(chunk)} -> {"ok" if "result" in res else res}')

    back = api('datastoreGet', {'authKey': KEY, 'collection': 'libraryItem', 'all': True})['result']
    flagged = [i for i in back if (i.get('state') or {}).get('flaggedWatched')]
    print(f'library after: {len(back)} items | flaggedWatched: {len(flagged)}')
    for t in targets[:3]:
        m = [i for i in back if i['_id'] == t['id']] or [i for i in back if (norm(i.get('name')), i.get('type')) == (norm(t['name']), t['type'])]
        print(f'  {t["id"]:12} {t["name"][:28]:30} -> ' + (f'_id={m[0]["_id"]} watched={(m[0]["state"] or {}).get("flaggedWatched")}' if m else 'MISSING'))

    if not TEST:
        after = os.path.join(BK, f'library-after-markwatched-{stamp}.json')
        json.dump(back, open(after, 'w'), indent=1)
        undo = os.path.join(BK, 'restore_library.py')
        open(undo, 'w').write(f'''#!/usr/bin/env python3
"""UNDO the Netflix watched-import: put the library back exactly as it was."""
import json, os, urllib.request
REPO = '{REPO}'
KEY = json.load(open(os.path.join(REPO, 'config.json')))['stremio_authkey']
SNAP = {before!r}
items = json.load(open(SNAP))
req = urllib.request.Request('{API}datastorePut',
    data=json.dumps({{'authKey': KEY, 'collection': 'libraryItem', 'changes': items}}).encode(),
    headers={{'content-type': 'application/json'}})
print('restored:', len(items), 'items ->', json.load(urllib.request.urlopen(req, timeout=180)).keys())
''')
        print(f'after-backup: {after}\nundo script: {undo}  (python3 {undo})')


if __name__ == '__main__':
    main()

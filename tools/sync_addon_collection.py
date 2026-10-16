#!/usr/bin/env python3
"""Sync the published addon manifest into Taaran's Stremio addon collection.

Reads the auth key from the repo config (never printed), backs up the current
collection, refreshes only OUR addon's manifest/version, and reads back.
"""
import json, os, shutil, sys, time, urllib.request

REPO = '/Users/taaran/Code/whatsapp-stremio-watchlist'
OUR_ID = 'in.taaran.whatsapp-watchlist'
MANIFEST_URL = 'https://taaranreddy.github.io/stremio-watchlist/manifest.json'
API = 'https://api.strem.io/api/'
BK = os.path.expanduser('~/Desktop/requests/Stremio-Audit')

cfg = json.load(open(os.path.join(REPO, 'config.json')))
key = cfg['stremio_authkey']


def call(method, payload, timeout=45):
    req = urllib.request.Request(API + method,
                                 data=json.dumps(payload).encode(),
                                 headers={'content-type': 'application/json',
                                          'user-agent': 'stremio-addon-sync'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fresh_manifest():
    req = urllib.request.Request(MANIFEST_URL + f'?cb={int(time.time())}',
                                 headers={'user-agent': 'stremio-addon-sync'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


cur = call('addonCollectionGet', {'authKey': key, 'update': True})['result']
addons = cur['addons']
os.makedirs(BK, exist_ok=True)
stamp = time.strftime('%Y%m%d-%H%M%S')
bk = os.path.join(BK, f'addonCollection-before-{stamp}.json')
json.dump(cur, open(bk, 'w'), indent=1)
print(f'backup: {bk}')
print(f'addons before: {len(addons)}')

man = fresh_manifest()
print(f"live manifest: {man['id']} v{man['version']} ({len(man['catalogs'])} catalogs)")

touched = 0
for a in addons:
    m = a.get('manifest') or {}
    if m.get('id') == OUR_ID or 'stremio-watchlist' in (a.get('transportUrl') or ''):
        a['manifest'] = man
        a['transportUrl'] = MANIFEST_URL
        a['flags'] = a.get('flags') or {'official': False, 'protected': False}
        touched += 1
print(f'patched addons: {touched}')
if not touched:
    sys.exit('our addon was not in the collection — nothing changed')

res = call('addonCollectionSet', {'authKey': key, 'addons': addons})
print('set result keys:', list(res.keys()))

back = call('addonCollectionGet', {'authKey': key, 'update': False})['result']['addons']
print(f'addons after: {len(back)}')
for a in back:
    m = a.get('manifest') or {}
    if m.get('id') == OUR_ID:
        print(f"  ours: v{m.get('version')} '{m.get('name')}' catalogs={len(m.get('catalogs', []))}")
        for c in m.get('catalogs', []):
            print(f"     - {c['type']:6} {c['id']:14} {c['name']}")

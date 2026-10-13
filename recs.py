"""'For You' recommendations from what Taaran actually watches. Stdlib only, no keys, no LLM.

Seeds = Stremio library items watched ≥10 min in the last 120 days + the WhatsApp watchlist.
Each seed votes for IMDb's own "More like this" titles, weighted by recency; the IMDb rating
breaks ties. Already-watched / already-listed titles are excluded.
"""
import json, math, time, urllib.request
from datetime import datetime, timezone

IMDB = 'https://caching.graphql.imdb.com/'
TYPES = {'movie': 'movie', 'tvSeries': 'series', 'tvMiniSeries': 'series'}
RPDB = 'https://api.ratingposterdb.com/t0-free-rpdb/imdb/poster-default/{}.jpg'  # free tier: poster with IMDb ★ badge


def _post(url, body, headers=None, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={'content-type': 'application/json', 'user-agent': 'Mozilla/5.0', **(headers or {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def imdb(query):
    return _post(IMDB, {'query': query}, {'x-imdb-client-name': 'imdb-web-next'})['data']

def library(authkey):
    r = _post('https://api.strem.io/api/datastoreGet', {'authKey': authkey, 'collection': 'libraryItem', 'ids': [], 'all': True})
    return r.get('result', [])

def _ts(s):
    try: return datetime.fromisoformat(s.replace('Z', '+00:00')[:26] + '+00:00' if '.' in s else s.replace('Z', '+00:00')).timestamp()
    except Exception: return 0

def seeds(lib, watchlist, days=120):
    now, out = time.time(), {}
    for i in lib:
        st = i.get('state') or {}
        if not i['_id'].startswith('tt') or (st.get('timeWatched') or 0) < 600_000: continue
        age = (now - _ts(st.get('lastWatched') or i.get('_mtime') or '')) / 86400
        if age > days: continue
        out[i['_id']] = (i['name'], math.exp(-age / 45))          # half-weight after ~1 month
    for w in watchlist:                                           # things he flagged count strongly
        out.setdefault(w['id'], (w['name'], 0.9))
    return out

FIELDS = 'id titleText{text} titleType{id} releaseYear{year endYear} ratingsSummary{aggregateRating voteCount} primaryImage{url} genres{genres{text}} plot{plotText{plainText}}'

def ratings(ids):
    """IMDb rating/genres/plot for our own ids (watchlist enrichment)."""
    if not ids: return {}
    q = '{ titles(ids:%s){ %s } }' % (json.dumps(list(ids)), FIELDS)
    return {t['id']: t for t in imdb(q)['titles'] if t}

def recommend(lib, watchlist, per_type=40):
    sd = seeds(lib, watchlist)
    if not sd: return {'movie': [], 'series': []}
    seen = {i['_id'] for i in lib} | {w['id'] for w in watchlist}
    votes, because, info = {}, {}, {}
    ids = list(sd)
    for k in range(0, len(ids), 25):
        q = '{ titles(ids:%s){ id moreLikeThisTitles(first:12){ edges{ node{ %s } } } } }' % (json.dumps(ids[k:k + 25]), FIELDS)
        for t in imdb(q)['titles']:
            if not t: continue
            name, w = sd[t['id']]
            for rank, e in enumerate(t['moreLikeThisTitles']['edges']):
                n = e['node']; r = n.get('ratingsSummary') or {}
                if n['id'] in seen or n['titleType']['id'] not in TYPES: continue
                if (r.get('voteCount') or 0) < 3000: continue
                votes[n['id']] = votes.get(n['id'], 0) + w * (1 - rank * 0.04)
                because.setdefault(n['id'], []).append((w, name)); info[n['id']] = n
    def score(tid):
        r = (info[tid].get('ratingsSummary') or {}).get('aggregateRating') or 6
        return votes[tid] * (0.6 + (r - 5) * 0.12)
    out = {'movie': [], 'series': []}
    for tid in sorted(votes, key=score, reverse=True):
        typ = TYPES[info[tid]['titleType']['id']]
        if len(out[typ]) < per_type:
            src = [n for _, n in sorted(because[tid], reverse=True)[:2]]
            out[typ].append(meta(info[tid], typ, f'Because you watched {" & ".join(src)}'))
    return out

def meta(n, typ, why=''):
    r = n.get('ratingsSummary') or {}; y = n.get('releaseYear') or {}
    rating = r.get('aggregateRating')
    year = f"{y.get('year')}" + (f"–{y['endYear'] or ''}" if typ == 'series' and y.get('year') else '') if y.get('year') else None
    plot = ((n.get('plot') or {}).get('plotText') or {}).get('plainText') or ''
    head = ' · '.join(x for x in [f'⭐ {rating} IMDb' if rating else '', why] if x)
    return {'id': n['id'], 'type': typ, 'name': n['titleText']['text'], 'poster': RPDB.format(n['id']),
            'imdbRating': str(rating) if rating else None, 'releaseInfo': year,
            'genres': [g['text'] for g in ((n.get('genres') or {}).get('genres') or [])],
            'description': f'{head}\n\n{plot}'.strip()}

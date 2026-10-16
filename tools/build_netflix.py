#!/usr/bin/env python3
"""Clean rebuild of the Netflix history: canonical title list -> IMDb ids.

Guarantees the final set is exactly the canonical (name,type) list derived
from the raw viewing rows — junk keys left by an earlier aborted run are
dropped, and anything still unresolved gets one smarter retry pass.

Outputs:
  netflix-resolved.json  (rebuilt, clean)
  netflix-catalog.json   ([{id,type,name,poster,year,last_seen}] newest first)
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = '/Users/taaran/Code/whatsapp-stremio-watchlist'
sys.path.insert(0, REPO)
import watchlist as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SEASON = re.compile(r'^(?P<n>.+?):\s*Season\s+\d+', re.I)
EPISODE = re.compile(r'^(?P<n>.+?):\s*["“\']')
PART = re.compile(r'^(?P<n>.+?):\s*Part\s+\d+', re.I)
LANG = re.compile(r'\s*\((?:Hindi|English|Telugu|Tamil|Malayalam|Kannada|Original|Dubbed|Subbed)\)\s*$', re.I)
TRIM = re.compile(r'\s*[:\-–]\s*(Limited Series|Collection|The Making of.*|A Netflix.*|Part \d+)\s*$', re.I)


def iso(d):
    try:
        dd, mm, yy = d.split('/')
        return f'20{yy}-{mm.zfill(2)}-{dd.zfill(2)}'
    except Exception:
        return ''


def canonical():
    """[(key, name, hint, last_seen)] newest-first, one per canonical title."""
    out, seen = [], set()
    for title, date in json.load(open(os.path.join(HERE, 'netflix-viewing.json'))):
        t = LANG.sub('', (title or '').strip()).strip()
        if not t:
            continue
        name, hint = t, 'movie'
        for rx in (SEASON, EPISODE, PART):
            m = rx.match(t)
            if m:
                name, hint = m.group('n').strip(), 'series'
                break
        # collapse accidental 'X: X' doubling
        if ': ' in name:
            a, b = name.split(': ', 1)
            if a.strip() and b.strip().startswith(a.strip()):
                name = b.strip()
        key = f'{hint}|{name.lower()}'
        if key in seen:
            continue
        seen.add(key)
        out.append((key, name, hint, iso(date)))
    return out


def attempt(name, hint, strict=True):
    """One resolve attempt; returns a partial record or None."""
    cands = [name]
    t = TRIM.sub('', name).strip()
    if t and t != name:
        cands.append(t)
    if ': ' in name:
        cands.append(name.split(': ', 1)[0].strip())
        cands.append(name.split(': ', 1)[1].strip())
    for c in cands:
        try:
            hit = W.search(c, hint if hint == 'series' else None)
        except Exception:
            continue
        if not hit:
            continue
        score, typ, meta, sim = hit
        if sim >= (0.75 if strict else 0.55) and not W.is_junk(meta):
            return {'id': meta['id'], 'type': typ, 'name': meta['name'],
                    'year': meta.get('releaseInfo') or meta.get('year'),
                    'poster': meta.get('poster'), 'score': round(score, 3), 'matched_on': c}
    return None


def main():
    canon = canonical()
    print(f'canonical titles: {len(canon)}', flush=True)
    out = os.path.join(HERE, 'netflix-resolved.json')
    old = {r['key']: r for r in json.load(open(out))} if os.path.exists(out) else {}
    recs, todo = {}, []
    for key, name, hint, date in canon:
        got = old.get(key)
        if got and got.get('id'):
            recs[key] = got
        else:
            todo.append((key, name, hint, date))
    print(f'  already resolved: {len(recs)} | to (re)resolve: {len(todo)}', flush=True)

    def work(item):
        key, name, hint, date = item
        r = attempt(name, hint, strict=True) or attempt(name, hint, strict=False)
        return key, name, hint, date, r

    if todo:
        with ThreadPoolExecutor(max_workers=12) as ex:
            for f in as_completed([ex.submit(work, t) for t in todo]):
                key, name, hint, date, r = f.result()
                rec = {'key': key, 'query': name, 'type_hint': hint, 'last_seen': date,
                       'id': None, 'type': None, 'name': None, 'year': None, 'poster': None}
                if r:
                    rec.update(r)
                else:
                    rec['note'] = 'no match'
                recs[key] = rec
    json.dump(list(recs.values()), open(out, 'w'), indent=1)

    order = {k: i for i, (k, *_rest) in enumerate(canon)}
    cat, seenids = [], set()
    for key, name, hint, date in canon:
        r = recs.get(key) or {}
        if not r.get('id') or r['id'] in seenids:
            continue
        seenids.add(r['id'])
        cat.append({'id': r['id'], 'type': r['type'], 'name': r['name'],
                    'poster': r['poster'], 'year': r['year'], 'last_seen': r['last_seen']})
    json.dump(cat, open(os.path.join(HERE, 'netflix-catalog.json'), 'w'), indent=1)
    miss = [f"{r['type_hint']} {r['query']}" for r in recs.values() if not r.get('id')]
    print(f'catalog: {len(cat)} items ({sum(1 for c in cat if c["type"]=="movie")} movies, '
          f'{sum(1 for c in cat if c["type"]=="series")} series)', flush=True)
    print(f'unmatched: {len(miss)}: {miss[:20]}', flush=True)


if __name__ == '__main__':
    main()

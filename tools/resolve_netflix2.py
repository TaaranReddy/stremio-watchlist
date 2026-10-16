#!/usr/bin/env python3
"""Resolve Netflix profile-A history to IMDb ids — parallel, episode-aware.

Key fix vs v1: Netflix lists series rows as
    'Dexter: Season 1: "Shrink Wrap"'   (season form)
    'Dexter: "Shrink Wrap"'             (bare episode form)
Both collapse to the series name, so we never search episode strings
(those were taking 15-30s each and matching junk).
"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = '/Users/taaran/Code/whatsapp-stremio-watchlist'
sys.path.insert(0, REPO)
import watchlist as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = os.path.join(HERE, 'netflix-viewing.json')
OUT = os.path.join(HERE, 'netflix-resolved.json')

SEASON = re.compile(r'^(?P<n>.+?):\s*Season\s+\d+', re.I)
EPISODE = re.compile(r'^(?P<n>.+?):\s*["“\']')            # 'Show: "Episode Title"'
PART = re.compile(r'^(?P<n>.+?):\s*Part\s+\d+', re.I)
LANG = re.compile(r'\s*\((?:Hindi|English|Telugu|Tamil|Malayalam|Kannada|Original|Dubbed|Subbed)\)\s*$', re.I)


def iso(d):
    try:
        dd, mm, yy = d.split('/')
        return f'20{yy}-{mm.zfill(2)}-{dd.zfill(2)}'
    except Exception:
        return ''


def entries():
    """[(name, type_hint, iso_date)] newest-first, one per title."""
    out, seen = [], set()
    for title, date in json.load(open(ROWS)):
        t = LANG.sub('', (title or '').strip()).strip()
        if not t:
            continue
        name, hint = t, 'movie'
        for rx in (SEASON, EPISODE, PART):
            m = rx.match(t)
            if m:
                name, hint = m.group('n').strip(), 'series'
                break
        if (name.lower(), hint) in seen:
            continue
        seen.add((name.lower(), hint))
        out.append((name, hint, iso(date)))
    return out


def one(item):
    name, hint, date = item
    key = f'{hint}|{name.lower()}'
    rec = {'key': key, 'query': name, 'type_hint': hint, 'last_seen': date,
           'id': None, 'type': None, 'name': None, 'year': None, 'poster': None, 'score': None}
    try:
        hit = W.search(name, hint if hint == 'series' else None)
        if hit and hit[3] >= 0.75:
            score, typ, meta, sim = hit
            if W.is_junk(meta):
                rec['note'] = 'junk genre'
            else:
                rec.update(id=meta['id'], type=typ, name=meta['name'],
                           year=meta.get('releaseInfo') or meta.get('year'),
                           poster=meta.get('poster'), score=round(score, 3))
    except Exception as e:
        rec['note'] = f'error {type(e).__name__}'
    return key, rec


def main():
    todo = entries()
    done = {}
    if os.path.exists(OUT):
        done = {r['key']: r for r in json.load(open(OUT))}
    todo = [t for t in todo if f'{"series" if t[1]=="series" else "movie"}|{t[0].lower()}' not in done]
    print(f'{len(todo)} titles to resolve ({len(done)} already cached)', flush=True)
    n = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(one, t): t for t in todo}
        for f in as_completed(futs):
            try:
                key, rec = f.result()
            except Exception as e:
                print('  !!', type(e).__name__, str(e)[:60], flush=True)
                continue
            done[key] = rec
            n += 1
            if n % 25 == 0:
                json.dump(list(done.values()), open(OUT, 'w'), indent=1)
                ok = sum(1 for r in done.values() if r.get('id'))
                print(f'  {n}/{len(todo)}  resolved {ok}/{len(done)}', flush=True)
    json.dump(list(done.values()), open(OUT, 'w'), indent=1)
    ok = sum(1 for r in done.values() if r.get('id'))
    print(f'DONE {ok}/{len(done)} resolved -> {OUT}', flush=True)


if __name__ == '__main__':
    main()

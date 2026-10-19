#!/usr/bin/env python3
"""WhatsApp "🎬 Watchlist" group → Stremio catalog addon. Stdlib only, zero cloud tokens.

Drop anything in the group: a title or list of titles, an IMDb link, a screenshot/poster, a clip,
or an Instagram/TikTok/YouTube reel. ident.py reads ALL the evidence (caption, Apple OCR of the frames,
Whisper transcript, key frames) and adds every title the post recommends — see ident.py.
  "-rocky" / "remove rocky" → removes the item
The bot confirms in the group with a 🤖-prefixed reply (those are ignored on re-read).
Stremio shows two rows on Board/Discover: "🎬 WhatsApp Watchlist" (Movies / Series).
"""
import base64, difflib, json, os, re, subprocess, threading, time, urllib.parse, urllib.request
import recs, ident
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser('~')
DIR = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(DIR, 'config.json')))
GROUP, TOKEN, PORT = CFG['group'], CFG['token'], CFG.get('port', 7171)
BRIDGE = 'http://127.0.0.1:3000'
DB = os.path.join(DIR, 'watchlist.json')
MEDIA = os.path.join(DIR, 'media'); os.makedirs(MEDIA, exist_ok=True)
NODE = f'{HOME}/.hermes/node/bin/node'
HELPER = f'{HOME}/.hermes/scripts/whatsapp-bridge/watchlist-media.mjs'
BOT = '🤖'
lock = threading.Lock()


def load():
    try: return json.load(open(DB))
    except Exception: return {'items': [], 'seen': []}

def save(db):
    tmp = DB + '.tmp'; json.dump(db, open(tmp, 'w'), indent=1, ensure_ascii=False); os.replace(tmp, DB)

def publish(db):
    """Static addon in docs/ → GitHub Pages, so Apple TV/phone work with no tunnel or server."""
    docs = os.path.join(DIR, 'docs'); os.makedirs(f'{docs}/catalog/movie', exist_ok=True); os.makedirs(f'{docs}/catalog/series', exist_ok=True)
    json.dump(MANIFEST, open(f'{docs}/manifest.json', 'w'), ensure_ascii=False)
    for typ in ('movie', 'series'):
        json.dump(catalog(db['items'], typ), open(f'{docs}/catalog/{typ}/wa-watchlist.json', 'w'), ensure_ascii=False)
        json.dump({'metas': db.get('recs', {}).get(typ, [])}, open(f'{docs}/catalog/{typ}/for-you.json', 'w'), ensure_ascii=False)
        for mode in ('recent', 'watched', 'continue'):
            json.dump(library_catalog(db.get('library', []), typ, mode),
                      open(f'{docs}/catalog/{typ}/{mode}.json', 'w'), ensure_ascii=False)
        json.dump(netflix_catalog(typ), open(f'{docs}/catalog/{typ}/netflix.json', 'w'), ensure_ascii=False)
        json.dump(seen_catalog(db, typ), open(f'{docs}/catalog/{typ}/seen.json', 'w'), ensure_ascii=False)
    open(f'{docs}/.nojekyll', 'w').close()
    env = {**os.environ, 'DEVELOPER_DIR': '/Library/Developer/CommandLineTools'}  # Xcode licence prompt breaks /usr/bin/git
    git = lambda *a: subprocess.run(['git', '-C', DIR, *a], capture_output=True, text=True, timeout=60, env=env)
    git('add', 'docs'); 
    if git('diff', '--cached', '--quiet').returncode:
        git('commit', '-qm', 'watchlist update'); r = git('push', '-q', 'origin', 'main'); log('published', r.returncode, r.stderr.strip()[:200])
        if r.returncode == 0: threading.Thread(target=check_pages, daemon=True).start()

GH = '/opt/homebrew/bin/gh'
REPO = 'TaaranReddy/stremio-watchlist'

def check_pages(tries=3):
    """GitHub Pages sometimes fails a build when pushes land close together, leaving Stremio on a stale
    catalog with no error anywhere. Wait for the build of HEAD; if it errored, ask GitHub to rebuild."""
    head = subprocess.run(['git', '-C', DIR, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    for _ in range(tries):
        for _ in range(20):
            time.sleep(30)
            r = subprocess.run([GH, 'api', f'repos/{REPO}/pages/builds/latest', '--jq', '.status + " " + .commit'],
                               capture_output=True, text=True, timeout=60).stdout.split()
            if len(r) == 2 and r[1] == head and r[0] in ('built', 'errored'): break
        if len(r) == 2 and r[0] == 'built' and r[1] == head: log('pages built', head[:7]); return
        log('pages build', r, '→ requesting rebuild')
        subprocess.run([GH, 'api', '-X', 'POST', f'repos/{REPO}/pages/builds'], capture_output=True, timeout=60)
    log('pages still not built after retries', head[:7])

def catalog(items, typ):
    out = []
    for i in items:
        if i['type'] != typ: continue
        m = {'id': i['id'], 'type': typ, 'name': i['name'], 'poster': recs.RPDB.format(i['id']), 'releaseInfo': i.get('year')}
        if i.get('rating'): m['imdbRating'] = str(i['rating'])
        if i.get('genres'): m['genres'] = i['genres']
        m['description'] = ' · '.join(x for x in [f"⭐ {i['rating']} IMDb" if i.get('rating') else '', f"From WhatsApp: {i['src']}" if i.get('src') else 'From your WhatsApp list'] if x) + (f"\n{i['orig']}" if i.get('orig') else '') + (f"\n\n{i['plot']}" if i.get('plot') else '')
        out.append(m)
    return {'metas': out}

def library_catalog(items, typ, mode):
    """Reliable history rows sourced from Stremio's own synced library state."""
    out = []
    for i in sorted(items, key=lambda x: (x.get('state') or {}).get('lastWatched', ''), reverse=True):
        if i.get('type') != typ or not str(i.get('_id', '')).startswith('tt'):
            continue
        st = i.get('state') or {}
        seen = (st.get('flaggedWatched') or 0) > 0 or (st.get('timesWatched') or 0) > 0
        partial = (st.get('timeOffset') or 0) > 0 and not seen
        if mode == 'watched' and not seen:
            continue
        if mode == 'continue' and not partial:
            continue
        if mode == 'recent' and not st.get('lastWatched'):
            continue
        m = {
            'id': i['_id'], 'type': typ, 'name': i.get('name') or i['_id'],
            'poster': recs.RPDB.format(i['_id']),
        }
        if i.get('poster'): m['background'] = i['poster']
        if i.get('year'): m['releaseInfo'] = str(i['year'])
        label = {'watched': '✓ Watched', 'continue': '▶ Continue watching', 'recent': 'Recently watched'}[mode]
        m['description'] = label
        out.append(m)
    return {'metas': out}


NETFLIX_DB = os.path.join(DIR, 'netflix-watched.json')

def netflix_items():
    """Taaran's imported Netflix viewing history (profile A), newest first."""
    try:
        return json.load(open(NETFLIX_DB))
    except Exception:
        return []

def _nflx_meta(i, label):
    m = {'id': i['id'], 'type': i.get('type'), 'name': i.get('name') or i['id'],
         'poster': i.get('poster') or recs.RPDB.format(i['id']),
         'description': label + (f" · last watched {i['last_seen']}" if i.get('last_seen') else '')}
    if i.get('year'):
        m['releaseInfo'] = str(i['year'])
    return m

def netflix_catalog(typ):
    """What he watched on Netflix — imported from the profile's viewing activity."""
    return {'metas': [_nflx_meta(i, '🍿 Watched on Netflix')
                      for i in netflix_items() if i.get('type') == typ and str(i.get('id', '')).startswith('tt')]}

def seen_catalog(db, typ):
    """Everything he has seen: Stremio's own watched flags unioned with the Netflix import."""
    out, have = [], set()
    lib = sorted(db.get('library', []), key=lambda x: (x.get('state') or {}).get('lastWatched', ''), reverse=True)
    for i in lib:
        st = i.get('state') or {}
        if i.get('type') != typ or not str(i.get('_id', '')).startswith('tt'):
            continue
        if not ((st.get('flaggedWatched') or 0) > 0 or (st.get('timesWatched') or 0) > 0):
            continue
        have.add(i['_id'])
        out.append({'id': i['_id'], 'type': typ, 'name': i.get('name') or i['_id'],
                    'poster': recs.RPDB.format(i['_id']), 'description': '✓ Watched in Stremio'})
    for i in netflix_items():
        if i.get('type') != typ or not str(i.get('id', '')).startswith('tt') or i['id'] in have:
            continue
        have.add(i['id'])
        out.append(_nflx_meta(i, '🍿 Watched on Netflix'))
    return {'metas': out}


def refresh(db):
    """Enrich watchlist, rebuild history rows and For You recommendations."""
    try:
        info = recs.ratings([i['id'] for i in db['items'] if 'rating' not in i])
        for i in db['items']:
            if (n := info.get(i['id'])):
                i['rating'] = (n.get('ratingsSummary') or {}).get('aggregateRating')
                i['genres'] = [g['text'] for g in ((n.get('genres') or {}).get('genres') or [])]
                i['plot'] = ((n.get('plot') or {}).get('plotText') or {}).get('plainText') or ''
        lib = recs.library(CFG['stremio_authkey']) if CFG.get('stremio_authkey') else []
        db['library'] = lib
        db['recs'] = recs.recommend(lib, db['items']); db['recs_at'] = int(time.time())
        log('library', len(lib), 'recs', {k: len(v) for k, v in db['recs'].items()})
    except Exception as e: log('refresh failed', e)

def get(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={'content-type': 'application/json', 'user-agent': 'wa-watchlist'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def log(*a): print(time.strftime('%H:%M:%S'), *a, flush=True)
ident.log = log

# ─── identification ─────────────────────────────────────────────────────────
SOCIAL = re.compile(r'https?://(?:www\.)?(?:instagram\.com|tiktok\.com|youtube\.com|youtu\.be|x\.com|twitter\.com|facebook\.com|fb\.watch)/\S+', re.I)

# Titles that share a name with a real film/series but are never what someone means
# when they drop a poster in the group ("The Conversation" tt1926875 = a 2012 talk show).
JUNK_GENRES = {'talk-show', 'talk show', 'news', 'reality-tv', 'game-show'}

def is_junk(meta):
    g = {str(x).strip().lower() for x in ((meta or {}).get('genres') or [])}
    return bool(g & JUNK_GENRES)

def popularity(typ, tid):
    try: return get(f'https://v3-cinemeta.strem.io/meta/{typ}/{tid}.json')['meta'].get('popularities', {}).get('moviedb', 0) or 0
    except Exception: return 0

# ─── poll loop ──────────────────────────────────────────────────────────────
def reply(text, to=None):
    """Post in the group; with `to` (the original message) it's a WhatsApp reply quoted under that post."""
    body = {'chatId': GROUP, 'message': f'{BOT} {text}'}
    if to and to.get('id'):
        body['quoted'] = {'id': to['id'], 'fromMe': to.get('from') == 'me' or bool(to.get('fromMe')),
                          'participant': None if to.get('from') == 'me' else to.get('from'),
                          'text': (to.get('body') or '')[:200]}
    try: get(f'{BRIDGE}/send', body)
    except Exception as e: log('reply failed', e)

def add_item(db, typ, meta, how, src='', pos=0):
    """→ 'added' | 'dup' | 'junk'. Keeps one entry per title, drops talk-show/news namesakes."""
    if any(i['id'] == meta['id'] for i in db['items']): return 'dup'
    if is_junk(meta): return 'junk'
    twin = next((i for i in db['items'] if i['name'].lower() == meta['name'].lower() and i['type'] != typ), None)
    if twin:
        if not is_junk(twin) and popularity(twin['type'], twin['id']) >= popularity(typ, meta['id']): return 'dup'
        db['items'].remove(twin); log('dropped namesake', twin['name'], twin['type'], twin['id'])
    db['items'].insert(pos, {'id': meta['id'], 'type': typ, 'name': meta.get('display') or meta['name'], 'poster': meta.get('poster'),
                           'orig': meta['name'] if meta.get('display') else None, 'src': src,
                           'year': meta.get('releaseInfo') or meta.get('year'), 'added': int(time.time()), 'via': how,
                           'rating': meta.get('imdbRating'), 'genres': meta.get('genres') or []})
    log('added', meta['name'], typ, how)
    return 'added'

def label(typ, meta):
    """'Hitch (2005) · Movie'. Romaji-only IMDb names get the English title alongside."""
    name = meta['name'] if not meta.get('display') else f"{meta['display']} — {meta['name']}"
    year = str(meta.get('releaseInfo') or meta.get('year') or '').replace('–', '-').rstrip('-')
    return f'{name}{f" ({year})" if year else ""} · {"Series" if typ == "series" else "Movie"}'

STATUS = {'added': '✅ added', 'dup': '☑️ already on list', 'junk': '⛔ skipped (talk show/news namesake)'}
KIND = {'reel': 'link', 'image': 'image', 'clip': 'clip', 'text': 'message', 'imdb': 'link'}

def summary(groups):
    """One reply per post, in the post's own order:
         📌 <what the post is>
         1. Title (year) · Movie — ✅ added
       so it's obvious which post each answer belongs to and what happened to every title."""
    out = []
    for g in groups:
        head = g['label'] or ''
        rows = g['rows']
        if not rows:
            out.append((f'📌 {head}\n' if head else '') + f"❓ No movie or series found in this {KIND.get(g['how'], 'post')}. Reply with the title and I'll add it.")
            continue
        if len(rows) == 1 and not head:
            out.append(f'{rows[0][0]} — {STATUS[rows[0][1]]}'); continue
        lines = [f'📌 {head}' + (f' · {len(rows)} titles' if len(rows) > 1 else '')] if head else []
        lines += [f'{n}. {t} — {STATUS[st]}' if len(rows) > 1 else f'{t} — {STATUS[st]}' for n, (t, st) in enumerate(rows, 1)]
        out.append('\n'.join(lines))
    return '\n\n'.join(out)

def chat_context(msgs, upto, n=12):
    prev = [x for x in msgs if x.get('timestamp', 0) < upto.get('timestamp', 0)][-n:]
    return '\n'.join(('[image]' if x.get('hasMedia') and not (x.get('body') or '').strip('[]imagevido') else (x.get('body') or ''))[:300]
                     for x in prev if (x.get('body') or x.get('hasMedia')))

def remove_titles(db, names):
    gone = []
    for q in names:
        hit = max(db['items'], default=None, key=lambda i: ident._sim(q, i['name']))
        if hit and ident._sim(q, hit['name']) > 0.6:
            db['items'].remove(hit); gone.append(hit['name'])
    return gone

def process(db, m, msgs=()):
    body = (m.get('body') or '').strip()
    if body.startswith(BOT) or body.startswith('[reaction'): return
    caption = '' if body in ('[image]', '[video]') else body
    say = lambda text: reply(text, m)
    if body.startswith('-') and len(body) > 2 and not m.get('hasMedia'):   # quick removal: "-rocky"
        gone = remove_titles(db, [body[1:].strip()])
        say(f'🗑️ Removed {", ".join(gone)}' if gone else f'Nothing on the list matches "{body[1:].strip()}".')
        return
    groups = []          # [{label, how, found}] — one per link / image / text part, in message order
    if m.get('hasMedia'):
        try: path = subprocess.run([NODE, HELPER, m['id'], MEDIA], capture_output=True, text=True, timeout=180,
                                   cwd=os.path.dirname(HELPER)).stdout.strip()
        except Exception: path = ''
        if not path: say("❓ Couldn't download that file from WhatsApp — send it again or type the title."); return
        how = 'clip' if path.endswith('.mp4') else 'image'
        found, lab = (ident.identify_video if how == 'clip' else ident.identify_image)(path, caption)
        groups.append({'label': lab if len(found) > 1 else '', 'src': lab, 'how': how, 'found': found})
    elif (links := SOCIAL.findall(body)) and not re.search(r'tt\d{6,9}', body):
        for u in links:
            found, lab = ident.identify_reel(u)
            groups.append({'label': lab or 'Shared post', 'how': 'reel', 'found': found})
        rest = SOCIAL.sub(' ', body).strip()
        if len(rest) > 3:   # "…/reel/xyz and also add Dune" → handle the text part too
            action, titles = ident.read_text(rest, chat_context(msgs, m))
            if action == 'add': groups.append({'label': f'Your text: "{rest[:60]}"', 'how': 'text', 'found': ident.resolve_all(titles)})
    elif (tt := re.search(r'tt\d{6,9}', body)):
        r = ident.cinemeta('movie', tt.group()); groups.append({'label': '', 'how': 'imdb', 'found': [r] if r else []})
    elif body:
        action, titles = ident.read_text(body, chat_context(msgs, m))
        if action == 'none': return            # chit-chat / feedback: stay quiet
        if action == 'remove':
            gone = remove_titles(db, [t.get('title', '') for t in titles])
            say(f'🗑️ Removed {", ".join(gone)}' if gone else "Couldn't find those on the list."); return
        groups.append({'label': '', 'how': 'text', 'found': ident.resolve_all(titles)})
    else: return
    pos = 0
    for g in groups:
        g['rows'] = []
        for typ, meta in g['found']:
            st = add_item(db, typ, meta, g['how'], g.get('src') or g['label'] or (body[:60] if g['how'] == 'text' else ''), pos)
            pos += st == 'added'
            g['rows'].append((label(typ, meta), st))
    say(summary(groups))

def poll():
    first = not os.path.exists(DB)
    while True:
        try:
            msgs = sorted(get(f'{BRIDGE}/chat/{urllib.parse.quote(GROUP)}/messages'), key=lambda m: m.get('timestamp', 0))
            with lock:
                db = load(); seen = set(db['seen']); before = json.dumps(db['items'])
                for m in msgs:
                    if m['id'] in seen: continue
                    seen.add(m['id'])
                    if not first:
                        try: process(db, m, msgs)
                        except Exception as e: log('process error', m['id'], e)
                db['seen'] = list(seen)[-2000:]; save(db); first = False
                changed = json.dumps(db['items']) != before
                if changed or time.time() - db.get('recs_at', 0) > CFG.get('recs_every_s', 6 * 3600):
                    refresh(db); save(db); publish(db)
        except Exception as e: log('poll error', e)
        time.sleep(CFG.get('poll_s', 45))

# ─── Stremio addon ──────────────────────────────────────────────────────────
MANIFEST = {
    'id': 'in.taaran.whatsapp-watchlist', 'version': '1.3.0', 'name': '🎬 Taaran Library',
    'description': 'Reliable Stremio history, Continue Watching, completed titles, Netflix import, WhatsApp Watchlist and For You picks.',
    'resources': ['catalog'], 'types': ['movie', 'series'], 'idPrefixes': ['tt'],
    'catalogs': [
        {'type': 'movie', 'id': 'continue', 'name': '▶️ Continue Watching'},
        {'type': 'series', 'id': 'continue', 'name': '▶️ Continue Watching'},
        {'type': 'movie', 'id': 'watched', 'name': '✅ Watched Movies'},
        {'type': 'series', 'id': 'watched', 'name': '✅ Watched Series'},
        {'type': 'movie', 'id': 'seen', 'name': '👀 Everything Seen'},
        {'type': 'series', 'id': 'seen', 'name': '👀 Everything Seen'},
        {'type': 'movie', 'id': 'netflix', 'name': '🍿 Watched on Netflix'},
        {'type': 'series', 'id': 'netflix', 'name': '🍿 Watched on Netflix'},
        {'type': 'movie', 'id': 'recent', 'name': '🕘 Recently Watched'},
        {'type': 'series', 'id': 'recent', 'name': '🕘 Recently Watched'},
        {'type': 'movie', 'id': 'wa-watchlist', 'name': '🎬 WhatsApp Watchlist'},
        {'type': 'series', 'id': 'wa-watchlist', 'name': '🎬 WhatsApp Watchlist'},
        {'type': 'movie', 'id': 'for-you', 'name': '✨ For You'},
        {'type': 'series', 'id': 'for-you', 'name': '✨ For You'},
    ],
    'behaviorHints': {'configurable': False},
}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header('content-type', 'application/json')
        self.send_header('access-control-allow-origin', '*'); self.send_header('cache-control', 'max-age=60')
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        parts = self.path.split('?')[0].strip('/').split('/')
        if not parts or parts[0] != TOKEN: return self.send({'err': 'not found'}, 404)
        rest = '/'.join(parts[1:])
        if rest == 'manifest.json': return self.send(MANIFEST)
        if (mt := re.fullmatch(r'catalog/(movie|series)/(wa-watchlist|for-you|recent|watched|continue|netflix|seen)(?:/[^/]*)?\.json', rest)):
            with lock: db = load()
            cat = mt.group(2)
            if cat == 'for-you': return self.send({'metas': db.get('recs', {}).get(mt.group(1), [])})
            if cat == 'netflix': return self.send(netflix_catalog(mt.group(1)))
            if cat == 'seen': return self.send(seen_catalog(db, mt.group(1)))
            if cat in ('recent', 'watched', 'continue'):
                return self.send(library_catalog(db.get('library', []), mt.group(1), cat))
            return self.send(catalog(db['items'], mt.group(1)))
        self.send({'err': 'not found'}, 404)

if __name__ == '__main__':
    _db = load(); refresh(_db); save(_db); publish(_db)
    threading.Thread(target=poll, daemon=True).start()
    log(f'addon on :{PORT}')
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()

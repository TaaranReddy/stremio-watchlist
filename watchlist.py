#!/usr/bin/env python3
"""WhatsApp "🎬 Watchlist" group → Stremio catalog addon. Stdlib only, zero cloud tokens.

Drop anything in the group: a title ("rocky"), an IMDb link, a caption, or a screenshot/poster.
  text/link  → Cinemeta search (Stremio's own free metadata API)
  image      → tesseract OCR → Cinemeta; no hit → local Ollama vision (qwen2.5vl:7b, ₹0)
  "-rocky"   → removes the item
The bot confirms in the group with a 🤖-prefixed reply (those are ignored on re-read).
Stremio shows two rows on Board/Discover: "🎬 WhatsApp Watchlist" (Movies / Series).
"""
import base64, difflib, json, os, re, subprocess, threading, time, urllib.parse, urllib.request
import recs
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
    open(f'{docs}/.nojekyll', 'w').close()
    env = {**os.environ, 'DEVELOPER_DIR': '/Library/Developer/CommandLineTools'}  # Xcode licence prompt breaks /usr/bin/git
    git = lambda *a: subprocess.run(['git', '-C', DIR, *a], capture_output=True, text=True, timeout=60, env=env)
    git('add', 'docs'); 
    if git('diff', '--cached', '--quiet').returncode:
        git('commit', '-qm', 'watchlist update'); r = git('push', '-q', 'origin', 'main'); log('published', r.returncode, r.stderr.strip()[:200])

def catalog(items, typ):
    out = []
    for i in items:
        if i['type'] != typ: continue
        m = {'id': i['id'], 'type': typ, 'name': i['name'], 'poster': recs.RPDB.format(i['id']), 'releaseInfo': i.get('year')}
        if i.get('rating'): m['imdbRating'] = str(i['rating'])
        if i.get('genres'): m['genres'] = i['genres']
        m['description'] = ' · '.join(x for x in [f"⭐ {i['rating']} IMDb" if i.get('rating') else '', 'From your WhatsApp list'] if x) + (f"\n\n{i['plot']}" if i.get('plot') else '')
        out.append(m)
    return {'metas': out}

def refresh(db):
    """Enrich watchlist with IMDb ★/genres and rebuild For You. ~2 s of free IMDb GraphQL, no tokens."""
    try:
        info = recs.ratings([i['id'] for i in db['items'] if 'rating' not in i])
        for i in db['items']:
            if (n := info.get(i['id'])):
                i['rating'] = (n.get('ratingsSummary') or {}).get('aggregateRating')
                i['genres'] = [g['text'] for g in ((n.get('genres') or {}).get('genres') or [])]
                i['plot'] = ((n.get('plot') or {}).get('plotText') or {}).get('plainText') or ''
        lib = recs.library(CFG['stremio_authkey']) if CFG.get('stremio_authkey') else []
        db['recs'] = recs.recommend(lib, db['items']); db['recs_at'] = int(time.time())
        log('recs', {k: len(v) for k, v in db['recs'].items()})
    except Exception as e: log('refresh failed', e)

def get(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={'content-type': 'application/json', 'user-agent': 'wa-watchlist'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def log(*a): print(time.strftime('%H:%M:%S'), *a, flush=True)

# ─── identification ─────────────────────────────────────────────────────────
def cinemeta_meta(tt):
    for typ in ('movie', 'series'):
        try:
            m = get(f'https://v3-cinemeta.strem.io/meta/{typ}/{tt}.json').get('meta')
            if m and m.get('name'): return typ, m
        except Exception: pass
    return None

def search(q, hint=None):
    """Best Cinemeta hit for q across movie+series, with a similarity score."""
    q = re.sub(r'\s+', ' ', q).strip()
    if len(q) < 2: return None
    per_type = {}
    for typ in ('movie', 'series'):
        try: metas = get(f'https://v3-cinemeta.strem.io/catalog/{typ}/top/search={urllib.parse.quote(q)}.json').get('metas', [])
        except Exception: continue
        for rank, m in enumerate(metas[:5]):
            name = m['name'].lower()
            sim = difflib.SequenceMatcher(None, q.lower(), name).ratio()
            if name.startswith(q.lower() + ':') or name.startswith(q.lower() + ' -'): sim = max(sim, 0.9)  # "Frieren: Beyond…"
            score = sim - rank * 0.03 + (0.1 if hint == typ else 0)
            if typ not in per_type or score > per_type[typ][0]: per_type[typ] = (score, typ, m, sim)
    cands = [c for c in per_type.values() if c[3] >= 0.5]
    if hint and hint in per_type and per_type[hint][3] >= 0.6: return per_type[hint]  # "anime"/"series"/"movie" said explicitly
    if len(cands) == 2 and not hint and abs(cands[0][0] - cands[1][0]) < 0.15:
        # movie-vs-series near tie ("frieren"): obscure namesakes lose to the popular title
        def pop(c):
            try: return get(f'https://v3-cinemeta.strem.io/meta/{c[1]}/{c[2]["id"]}.json')['meta'].get('popularities', {}).get('moviedb', 0) or 0
            except Exception: return 0
        return max(cands, key=pop)
    return max(per_type.values(), default=None, key=lambda c: c[0])

SOCIAL = re.compile(r'https?://(?:www\.)?(?:instagram\.com|tiktok\.com|youtube\.com|youtu\.be|x\.com|twitter\.com|facebook\.com|fb\.watch)/\S+', re.I)

def link_info(url):
    """Reel/short → (caption text, thumbnail path). yt-dlp first; og:image scrape for restricted posts."""
    text, thumb = '', ''
    try:
        r = subprocess.run(['yt-dlp', '--skip-download', '-J', url], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            j = json.loads(r.stdout); text = '\n'.join(x for x in [j.get('description') or '', j.get('title') or ''] if x)
            thumb = j.get('thumbnail') or ''
    except Exception as e: log('yt-dlp failed', e)
    if not thumb:
        try:
            req = urllib.request.Request(url, headers={'user-agent': 'facebookexternalhit/1.1'})
            html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
            for k in ('og:title', 'og:description'):
                if (m := re.search(rf'property="{k}" content="([^"]*)"', html)): text += '\n' + __import__('html').unescape(m.group(1))
            if (m := re.search(r'property="og:image" content="([^"]+)"', html)): thumb = m.group(1).replace('&amp;', '&')
        except Exception as e: log('og scrape failed', e)
    path = ''
    if thumb:
        try:
            path = os.path.join(MEDIA, re.sub(r'\W', '_', url)[-60:] + '.jpg')
            urllib.request.urlretrieve(thumb, path)
        except Exception: path = ''
    return text, path

def identify_link(url):
    text, path = link_info(url)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cands = []
    for l in lines[:15]:   # "🎬 The Notebook (2004)" / "Movie: X" / "#movietitle"
        if (m := re.search(r'(?:🎬|🎥|🍿|movie\s*[:\-]|film\s*[:\-]|series\s*[:\-]|title\s*[:\-])\s*([^\n(|#]{2,60}?)\s*(?:\((\d{4})\))?(?:$|[|#\n])', l, re.I)):
            cands.append(m.group(1))
        cands += re.findall(r'^\s*([A-Z][^\n()]{1,50}?)\s*\((\d{4})\)', l) and [re.findall(r'^\s*([A-Z][^\n()]{1,50}?)\s*\((\d{4})\)', l)[0][0]] or []
    for c in cands:
        if (b := search(c)) and b[3] >= 0.6: return (b[1], b[2]), 'reel-caption'
    if path:
        hits = [b for l in ocr_lines(path) if (b := search(l)) and b[3] >= 0.75]
        if hits: b = max(hits, key=lambda x: x[0]); return (b[1], b[2]), 'reel-ocr'
        if (g := vision_guess(path)) and (b := search(g)) and b[3] >= 0.5: return (b[1], b[2]), f'reel-vision:{g}'
    return None, 'reel'

def identify_text(text):
    tt = re.search(r'tt\d{6,9}', text)
    if tt and (r := cinemeta_meta(tt.group())): return r
    hint = 'series' if re.search(r'\b(series|show|season|anime|tv|s\d+e\d+)\b', text, re.I) else (
           'movie' if re.search(r'\bmovie|film\b', text, re.I) else None)
    clean = re.sub(r'https?://\S+|\b(watch|movie|film|series|show|anime|season \d+|recommend(ed)?|must|this)\b', ' ', text, flags=re.I)
    best = search(clean, hint)
    return (best[1], best[2]) if best and best[3] >= 0.5 else None

def ocr_lines(path):
    try: out = subprocess.run(['tesseract', path, '-', '--psm', '3'], capture_output=True, text=True, timeout=60).stdout
    except Exception: return []
    lines = [re.sub(r'[^\w\s:&\'-]', '', l).strip() for l in out.splitlines()]
    return sorted({l for l in lines if 3 <= len(l) <= 60 and sum(c.isalpha() for c in l) >= 3}, key=len, reverse=True)[:6]

def vision_guess(path):
    try:
        b64 = base64.b64encode(open(path, 'rb').read()).decode()
        r = get('http://127.0.0.1:11434/api/generate', {
            'model': CFG.get('vision_model', 'qwen2.5vl:7b'), 'stream': False, 'images': [b64],
            'options': {'num_predict': 30, 'temperature': 0},
            'prompt': 'This is a screenshot, poster or still from a movie, TV series or anime. '
                      'Reply with ONLY its official English title, nothing else. If unknown reply UNKNOWN.'}, timeout=180)
        g = r.get('response', '').strip().strip('"*.').splitlines()[0] if r.get('response') else ''
        return '' if 'UNKNOWN' in g.upper() else g
    except Exception as e:
        log('vision failed', e); return ''

def identify_image(msg_id, caption):
    if caption and caption not in ('[image]', '[video]') and (r := identify_text(caption)): return r, 'caption'
    try: path = subprocess.run([NODE, HELPER, msg_id, MEDIA], capture_output=True, text=True, timeout=60,
                               cwd=os.path.dirname(HELPER)).stdout.strip()
    except Exception: path = ''
    if not path: return None, 'no-media'
    hits = [b for l in ocr_lines(path) if (b := search(l)) and b[3] >= 0.75]
    if hits:
        b = max(hits, key=lambda x: x[0]); return (b[1], b[2]), 'ocr'
    if (g := vision_guess(path)) and (b := search(g)) and b[3] >= 0.5: return (b[1], b[2]), f'vision:{g}'
    return None, 'unrecognised'

# ─── poll loop ──────────────────────────────────────────────────────────────
def reply(text):
    try: get(f'{BRIDGE}/send', {'chatId': GROUP, 'message': f'{BOT} {text}'})
    except Exception as e: log('reply failed', e)

def process(db, m):
    body = (m.get('body') or '').strip()
    if body.startswith(BOT) or body.startswith('[reaction'): return
    if body.startswith('-') and len(body) > 2:   # removal
        q = body[1:].strip().lower()
        hit = max(db['items'], default=None, key=lambda i: difflib.SequenceMatcher(None, q, i['name'].lower()).ratio())
        if hit and difflib.SequenceMatcher(None, q, hit['name'].lower()).ratio() > 0.5:
            db['items'].remove(hit); reply(f'Removed {hit["name"]}')
        return
    if m.get('hasMedia'): res, how = identify_image(m['id'], body)
    elif body and (sm := SOCIAL.search(body)) and not re.search(r'tt\d{6,9}', body):
        rest = SOCIAL.sub(' ', body).strip()
        res, how = (identify_text(rest), 'text') if len(rest) > 2 else (None, '')
        if not res: res, how = identify_link(sm.group())
    elif body: res, how = identify_text(body), 'text'
    else: return
    if not res:
        reply(f"Couldn't identify that ({how}). Send the title as text and I'll add it."); return
    typ, meta = res
    if any(i['id'] == meta['id'] for i in db['items']):
        reply(f'Already on the list: {meta["name"]}'); return
    db['items'].insert(0, {'id': meta['id'], 'type': typ, 'name': meta['name'], 'poster': meta.get('poster'),
                           'year': meta.get('releaseInfo') or meta.get('year'), 'added': int(time.time()), 'via': how})
    log('added', meta['name'], typ, how)
    reply(f'Added to Stremio → {meta["name"]} ({meta.get("releaseInfo") or meta.get("year") or "?"}) · {"Series" if typ == "series" else "Movie"}')

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
                        try: process(db, m)
                        except Exception as e: log('process error', m['id'], e)
                db['seen'] = list(seen)[-2000:]; save(db); first = False
                changed = json.dumps(db['items']) != before
                if changed or time.time() - db.get('recs_at', 0) > CFG.get('recs_every_s', 6 * 3600):
                    refresh(db); save(db); publish(db)
        except Exception as e: log('poll error', e)
        time.sleep(CFG.get('poll_s', 45))

# ─── Stremio addon ──────────────────────────────────────────────────────────
MANIFEST = {
    'id': 'in.taaran.whatsapp-watchlist', 'version': '1.1.0', 'name': '🎬 WhatsApp Watchlist',
    'description': 'Your WhatsApp "🎬 Watchlist" group + For You picks from what you watch. IMDb ★ on every poster.',
    'resources': ['catalog'], 'types': ['movie', 'series'], 'idPrefixes': ['tt'],
    'catalogs': [{'type': 'movie', 'id': 'wa-watchlist', 'name': '🎬 WhatsApp Watchlist'},
                 {'type': 'series', 'id': 'wa-watchlist', 'name': '🎬 WhatsApp Watchlist'},
                 {'type': 'movie', 'id': 'for-you', 'name': '✨ For You'},
                 {'type': 'series', 'id': 'for-you', 'name': '✨ For You'}],
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
        if (mt := re.fullmatch(r'catalog/(movie|series)/(wa-watchlist|for-you)(?:/[^/]*)?\.json', rest)):
            with lock: db = load()
            if mt.group(2) == 'for-you': return self.send({'metas': db.get('recs', {}).get(mt.group(1), [])})
            return self.send(catalog(db['items'], mt.group(1)))
        self.send({'err': 'not found'}, 404)

if __name__ == '__main__':
    _db = load(); refresh(_db); save(_db); publish(_db)
    threading.Thread(target=poll, daemon=True).start()
    log(f'addon on :{PORT}')
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()

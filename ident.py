"""Turn a WhatsApp message (text / screenshot / reel link) into a list of real IMDb titles.

Pipeline, for every input type:
  1. gather evidence  — caption (yt-dlp, og-tags), Apple Vision OCR of frames, Whisper transcript, key frames
  2. read it          — one LLM pass (DeepSeek Flash, text+image) lists EVERY title the post actually names;
                        local Ollama Qwen3-VL is the fallback when DeepSeek is unreachable
  3. resolve          — each title → IMDb suggestion API (+ Cinemeta), checked against the year/type the
                        evidence gave, so a phrase like "For the Boys" can never become a 1991 film
"""
import base64, difflib, glob, json, os, re, shutil, subprocess, tempfile, unicodedata, urllib.parse, urllib.request

HOME = os.path.expanduser('~')
DIR = os.path.dirname(os.path.abspath(__file__))
VOCR = os.path.join(DIR, 'tools', 'vocr')
WHISPER_MODEL = f'{HOME}/asr-bench/models/ggml-large-v3-turbo.bin'
OLLAMA_VL = 'hf.co/Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF:Q4_K_M'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36'
log = print


def _json(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'content-type': 'application/json', 'user-agent': UA, **(headers or {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _deepseek_key():
    if os.environ.get('DEEPSEEK_API_KEY'): return os.environ['DEEPSEEK_API_KEY']
    try:
        for l in open(f'{HOME}/.hermes/.env'):
            if l.startswith('DEEPSEEK_API_KEY='): return l.split('=', 1)[1].strip().strip('"\'')
    except Exception: pass
    return ''


def _parse_json(s):
    s = (s or '').strip()
    m = re.search(r'\{.*\}', s, re.S)
    return json.loads(m.group() if m else s)


def llm(prompt, images=()):
    """JSON-mode call. DeepSeek first (fast, sees images); local Qwen3-VL if it fails."""
    key = _deepseek_key()
    if key:
        content = [{'type': 'text', 'text': prompt}] + [
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b}} for b in images]
        for _ in range(2):
            try:
                r = _json('https://api.deepseek.com/chat/completions', {
                    'model': 'deepseek-flash', 'max_tokens': 16000, 'temperature': 0, 'reasoning_effort': 'low',
                    'response_format': {'type': 'json_object'},
                    'messages': [{'role': 'user', 'content': content if images else prompt}]},
                    {'authorization': f'Bearer {key}'}, timeout=180)
                txt = r['choices'][0]['message'].get('content') or ''
                if txt.strip(): return _parse_json(txt)
                log('deepseek empty reply', r['choices'][0].get('finish_reason'))
            except Exception as e: log('deepseek failed', e)
    try:
        r = _json('http://127.0.0.1:11434/api/chat', {
            'model': OLLAMA_VL, 'stream': False, 'format': 'json', 'options': {'temperature': 0, 'num_predict': 1500},
            'messages': [{'role': 'user', 'content': prompt, 'images': list(images)[:4]}]}, timeout=600)
        return _parse_json(r['message']['content'])
    except Exception as e:
        log('ollama failed', e); return {}


# ─── evidence ───────────────────────────────────────────────────────────────
def ocr(paths):
    """Apple Vision OCR → list of line-lists, one per image."""
    if not paths: return []
    try: out = subprocess.run([VOCR, *paths], capture_output=True, text=True, timeout=180).stdout
    except Exception as e:
        log('vocr failed', e); return [[] for _ in paths]
    return [[l.strip() for l in blk.splitlines() if l.strip()] for blk in out.split('---\n')][:len(paths)]


UI_NOISE = re.compile(r'^(follow|\d{1,2}:\d{2}|[~\-\d\s.,k]+|gif|avi|reply|send|like|share|join the conversation.*|'
                      r'what do you think of this\??|\d+[hdmw]|\d+ (hours?|days?|weeks?) ago|by author|more)$', re.I)

def ocr_digest(blocks, limit=120):
    """Merge OCR from many frames: keep each distinct line once, most-frequent first (title cards repeat)."""
    count, order = {}, []
    for blk in blocks:
        for l in dict.fromkeys(blk):
            if UI_NOISE.match(l) or len(l) < 2: continue
            k = re.sub(r'\W+', '', l.lower())
            if k not in count: order.append((k, l)); count[k] = 0
            count[k] += 1
    rows = sorted(order, key=lambda kl: -count[kl[0]])[:limit]
    return '\n'.join(f'{l}' + (f'  [x{count[k]}]' if count[k] > 1 else '') for k, l in rows)


def transcribe(video):
    if not (os.path.exists(WHISPER_MODEL) and shutil.which('whisper-cli')): return ''
    wav = video + '.wav'
    try:
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', video, '-ar', '16000', '-ac', '1', wav], timeout=120, check=True)
        t = subprocess.run(['whisper-cli', '-m', WHISPER_MODEL, '-f', wav, '-nt', '-np', '-l', 'auto'],
                           capture_output=True, text=True, timeout=300).stdout
        t = re.sub(r'\s+', ' ', t).strip()
        # music-only reels make whisper hallucinate filler
        return '' if len(t) < 12 or re.fullmatch(r'[\[\(]?(music|♪|applause|silence|\W)*[\]\)]?', t, re.I) else t
    except Exception as e:
        log('whisper failed', e); return ''
    finally:
        if os.path.exists(wav): os.remove(wav)


def frames(video, outdir, n=16):
    try:
        dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', video],
                                   capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except Exception: dur = 0
    fps = max(n / dur, 0.2) if dur else 1
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', video, '-vf', f'fps={fps:.3f},scale=720:-2', '-frames:v', str(n),
                    os.path.join(outdir, 'f%02d.jpg')], timeout=180)
    return sorted(glob.glob(os.path.join(outdir, 'f*.jpg')))


def b64(path, width=640):
    """Downscaled JPEG as base64 (keeps the vision request small)."""
    out = path + '.s.jpg'
    try:
        subprocess.run(['sips', '-Z', str(width), '-s', 'format', 'jpeg', path, '--out', out], capture_output=True, timeout=30)
        return base64.b64encode(open(out if os.path.exists(out) else path, 'rb').read()).decode()
    finally:
        if os.path.exists(out): os.remove(out)


def og_tags(url):
    """Instagram serves og:title/description (= the caption) to link-preview bots even for login-walled posts."""
    out = {}
    try:
        req = urllib.request.Request(url, headers={'user-agent': 'facebookexternalhit/1.1'})
        html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
        import html as H
        for k in ('og:title', 'og:description', 'og:image'):
            if (m := re.search(rf'property="{k}" content="([^"]*)"', html)): out[k] = H.unescape(m.group(1))
    except Exception as e: log('og scrape failed', e)
    return out


def reel_evidence(url, work):
    """Everything we can learn about a reel/post: caption, OCR of frames/slides, speech, key frames."""
    ev = {'url': url, 'caption': '', 'uploader': '', 'ocr': '', 'transcript': '', 'images': []}
    info = None
    try:
        r = subprocess.run(['yt-dlp', '--skip-download', '-J', url], capture_output=True, text=True, timeout=90)
        if r.returncode == 0: info = json.loads(r.stdout)
    except Exception as e: log('yt-dlp meta failed', e)
    if info:
        ev['caption'] = info.get('description') or ''
        ev['uploader'] = info.get('uploader') or info.get('channel') or ''
    og = og_tags(url) if 'instagram.com' in url else {}
    if not ev['caption'] and og:
        ev['caption'] = '\n'.join(x for x in (og.get('og:title'), og.get('og:description')) if x)
    try:   # the actual media: video → frames + speech; carousel → every slide
        subprocess.run(['yt-dlp', '-q', '--no-warnings', '-f', 'b[height<=720]/bv*[height<=720]+ba/b', '--max-filesize', '150M',
                        '-o', os.path.join(work, 'm%(playlist_index|0)s.%(ext)s'), url], capture_output=True, timeout=240)
    except Exception as e: log('yt-dlp download failed', e)
    files = sorted(glob.glob(os.path.join(work, 'm*')))
    if not files and (thumb := (info or {}).get('thumbnail') or og.get('og:image')):
        try:
            p = os.path.join(work, 'thumb.jpg'); urllib.request.urlretrieve(thumb, p); files.append(p)
        except Exception: pass
    return media_evidence(files, work, ev)


def media_evidence(files, work, ev=None):
    """Frames + Apple OCR + Whisper for local videos/images."""
    ev = ev or {'url': '', 'caption': '', 'uploader': '', 'ocr': '', 'transcript': '', 'images': []}
    imgs = []
    for f in files:
        if f.lower().endswith(('.mp4', '.webm', '.mkv', '.mov', '.m4v')):
            sub = tempfile.mkdtemp(dir=work)
            imgs += frames(f, sub, 16 if len(imgs) < 16 else 4)
            if not ev['transcript']: ev['transcript'] = transcribe(f)
        elif f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.heic')):
            imgs.append(f)
    ev['ocr'] = ocr_digest(ocr(imgs))
    step = max(1, len(imgs) // 6)
    ev['images'] = [b64(p) for p in imgs[::step][:6]]
    return ev


# ─── reading the evidence ───────────────────────────────────────────────────
RULES = '''Rules:
- List EVERY film / TV series / anime the post RECOMMENDS: the subject of a review/breakdown, or each entry of a "top N" / ranking / carousel list (all of them, in order).
- Titles that only appear as CONTEXT do not count — e.g. a studio's or director's earlier works, "from the creator of X", "better than X", comparisons, credits screens. Mark those role "referenced".
- Only titles actually evidenced in the caption, on-screen text, speech, or clearly readable posters. Never guess from vibes.
- Hashtags, catchphrases and list headings are NOT titles ("Top 5 Rom Coms For the Boys", "for the boys", "must watch"). Account names are not titles.
- Give the official English title as listed on IMDb, the original/romaji title if shown, the release year if stated or certain, and type movie|series (anime TV = series).
- confidence = how sure you are this exact title is meant. Recognising a poster/still with no readable title text is rarely above 0.6.
- If nothing is identifiable, return an empty list.'''

SCHEMA = '{"titles":[{"title":"...","alt_title":"... or null","year":2004,"type":"movie|series","evidence":"caption|ocr|speech|poster","role":"recommended|referenced","confidence":0.0-1.0}]}'


def read_reel(ev):
    prompt = f'''You are reading an Instagram/TikTok/YouTube post someone shared to their watchlist group, to work out which titles it is about.

UPLOADER: {ev['uploader'] or '-'}
CAPTION:
{ev['caption'][:3000] or '-'}

ON-SCREEN TEXT (Apple OCR across frames/slides, deduped, [xN] = seen on N frames):
{ev['ocr'][:3000] or '-'}

SPEECH (Whisper transcript):
{ev['transcript'][:2000] or '-'}

{len(ev['images'])} key frames are attached.

{RULES}
Reply JSON only: {SCHEMA}'''
    return llm(prompt, ev['images']).get('titles') or []


def read_image(path, caption=''):
    lines = ocr([path])[0] if path else []
    prompt = f'''Someone dropped this screenshot/poster/still into their movie watchlist group.
{'Their caption: ' + caption if caption else ''}
ON-SCREEN TEXT (Apple OCR):
{ocr_digest([lines])[:3000] or '-'}

Which film / series / anime is this about? Use the OCR text first (screenshots of Instagram/Reddit posts usually name the title); otherwise recognise the poster/still.
{RULES}
Reply JSON only: {SCHEMA}'''
    return llm(prompt, [b64(path, 1024)] if path else []).get('titles') or []


def read_text(text, context):
    prompt = f'''You manage a personal movie/series watchlist fed from a WhatsApp group. Decide what the NEW message wants.

RECENT CHAT (oldest first; 🤖 lines are the bot's own replies):
{context or '-'}

NEW MESSAGE: {text}

Actions:
- "add": the message names one or more titles to watch (bare names, lists, "watch X", typos allowed — correct them). "Full series"/"all the movies"/"the whole franchise" refers to the most recent title in the chat → list every film in that franchise.
- "remove": the message asks to remove/delete titles from the list.
- "none": chit-chat, feedback or instructions to the bot that name no title.
{RULES}
Reply JSON only: {{"action":"add|remove|none","titles":[{{"title":"...","alt_title":null,"year":null,"type":"movie|series"}}]}}'''
    r = llm(prompt)
    return r.get('action') or 'none', r.get('titles') or []


# ─── resolving titles to IMDb ids ───────────────────────────────────────────
TYPES = {'movie': 'movie', 'tvMovie': 'movie', 'video': 'movie', 'tvSeries': 'series', 'tvMiniSeries': 'series', 'tvSpecial': 'movie'}

def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = s.replace('&', 'and')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', s)).strip()


def imdb_suggest(q):
    q = norm(q)
    if not q: return []
    try: d = _json(f'https://v3.sg.media-imdb.com/suggestion/x/{urllib.parse.quote(q)}.json', timeout=20).get('d', [])
    except Exception as e:
        log('imdb suggest failed', e); return []
    import html as H
    return [{'id': x['id'], 'name': H.unescape(x.get('l', '')), 'year': x.get('y'), 'type': TYPES[x['qid']], 'rank': x.get('rank') or 10**7}
            for x in d if str(x.get('id', '')).startswith('tt') and x.get('qid') in TYPES]


def cinemeta(typ, tt):
    for t in (typ, 'series' if typ == 'movie' else 'movie'):
        try:
            m = _json(f'https://v3-cinemeta.strem.io/meta/{t}/{tt}.json', timeout=20).get('meta')
            if m and m.get('name'):
                m['name'] = __import__('html').unescape(m['name']); return t, m
        except Exception: pass
    return None


def _sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b: return 0
    s = difflib.SequenceMatcher(None, a, b).ratio()
    if b.startswith(a + ' ') or a.startswith(b + ' '): s = max(s, 0.85)   # "Frieren" vs "Frieren: Beyond Journey's End"
    return s


def resolve(t):
    """{'title','alt_title','year','type'} → (type, cinemeta-meta) or None. Strict: title must really match."""
    want_y = int(t['year']) if str(t.get('year') or '').isdigit() else None
    want_t = t.get('type') if t.get('type') in ('movie', 'series') else None
    names = [n for n in (t.get('title'), t.get('alt_title')) if n]
    cands = {}
    for n in names:
        for c in imdb_suggest(n):
            sim = max(_sim(x, c['name']) for x in names)
            score = sim
            if want_y and c['year']: score += 0.25 if abs(c['year'] - want_y) <= 1 else -0.3
            if want_t: score += 0.1 if c['type'] == want_t else -0.15
            score -= min(c['rank'], 10**6) / 10**7   # popularity breaks ties between namesakes
            if sim >= 0.6 and (c['id'] not in cands or score > cands[c['id']][0]): cands[c['id']] = (score, c, sim)
    if not cands: return None
    score, c, sim = max(cands.values(), key=lambda x: x[0])
    if sim < 0.75 and not (want_y and c['year'] and abs(c['year'] - want_y) <= 1): return None
    r = cinemeta(c['type'], c['id'])
    if r: return r
    return c['type'], {'id': c['id'], 'name': c['name'], 'releaseInfo': str(c['year'] or ''), 'genres': []}


def resolve_all(titles):
    out, seen = [], set()
    for t in titles:
        if not isinstance(t, dict) or not t.get('title'): continue
        if t.get('role') == 'referenced': log('  skip referenced', t['title']); continue
        try: conf = float(t.get('confidence', 1))
        except Exception: conf = 1
        if conf < 0.7: log('  skip low-confidence', t['title'], conf); continue
        r = resolve(t)
        log('  resolve', t.get('title'), t.get('year'), '→', r and (r[1]['name'], r[1].get('releaseInfo'), r[1]['id']))
        if r and r[1]['id'] not in seen:
            seen.add(r[1]['id']); out.append(r)
    return out


def identify_reel(url):
    work = tempfile.mkdtemp(prefix='reel_')
    try:
        ev = reel_evidence(url, work)
        log('reel', url, 'caption', len(ev['caption']), 'ocr', len(ev['ocr']), 'speech', len(ev['transcript']), 'frames', len(ev['images']))
        return resolve_all(read_reel(ev))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def identify_image(path, caption=''):
    return resolve_all(read_image(path, caption))


def identify_video(path, caption=''):
    """A clip forwarded straight into the group (not a link)."""
    work = tempfile.mkdtemp(prefix='clip_')
    try:
        ev = media_evidence([path], work)
        ev['caption'] = caption
        return resolve_all(read_reel(ev))
    finally:
        shutil.rmtree(work, ignore_errors=True)

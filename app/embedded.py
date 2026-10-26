"""Bounded discovery of public, statically declared video sources.

No script execution, playback, credentials, access-control or DRM workarounds.
Unknown players need a dedicated adapter when they do not declare public sources.
"""
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit, urlunsplit

import httpx

from app.config import PROXY_URL
from app.plans import MediaProblem
from app.security import validate_url

MAX_PAGES = 4
MAX_DEPTH = 2
MAX_BYTES = 2 * 1024**2
MAX_CANDIDATES = 30
MEDIA = re.compile(r'\.(mp4|webm|m4v|mov|ogv|m3u8|mpd)(?:$|[?#])', re.I)
PLAYER_NAMES = {
    'Video.js': r'video(?:\.|-)js', 'JW Player': r'jwplayer|jw-player',
    'Plyr': r'\bplyr\b', 'Shaka Player': r'\bshaka\b', 'hls.js': r'\bhls(?:\.js|\.loadsource|\s*\()',
    'dash.js': r'\bdashjs\b', 'Clappr': r'\bclappr\b', 'PlayerJS': r'\bplayerjs\b',
    'Flowplayer': r'\bflowplayer\b', 'Fluid Player': r'\bfluidplayer\b',
    'Bitmovin': r'\bbitmovin\b', 'THEOplayer': r'\btheoplayer\b',
}
EMBED_HOSTS = ('youtube.com', 'youtube-nocookie.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
               'vk.com', 'vkvideo.ru', 'rutube.ru', 'player.kick.com', 'kodikplayer.com',
               'alloha.yani.tv', 'alloha.tv')


def clean_url(value, base):
    if not isinstance(value, str):
        return None
    value = html.unescape(value.strip()).replace('\\/', '/')
    if not value or value.startswith('#'): return None
    value = re.sub(r'\\u002[fF]', '/', value)
    value = re.sub(r'\\u0026', '&', value)
    try:
        return validate_url(urldefrag(urljoin(base, value))[0])
    except ValueError:
        return None


def known_embed(url):
    host = urlsplit(url).hostname or ''
    return any(host == domain or host.endswith('.' + domain) for domain in EMBED_HOSTS)


def media_identity(url):
    parsed = urlsplit(url)
    # Retain content identifiers; discard expiring signing parameters, not video identity.
    ids = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() in
           ('id', 'video', 'video_id', 'media_id', 'v', 'episode', 'season')]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, json.dumps(ids), ''))


class PlayerHTML(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.media = []
        self.frames = []
        self.scripts = []
        self.title = ''
        self.in_title = False
        self.in_video = 0
        self.script = None
        self.has_blob = False
        self.has_video = False

    def add_media(self, value, title='', explicit=False):
        # PlayerJS uses a single file string containing labelled quality alternatives.
        if isinstance(value, str) and re.match(r'^\s*\[\d{3,4}p?\]', value):
            for match in re.finditer(r'\[(\d{3,4}p?)\]([^\[]+)', value):
                self.add_media(match[2].strip().strip(','), str(title or '') + ' · ' + match[1], explicit)
            return
        if isinstance(value, str) and value.startswith('blob:'):
            self.has_blob = True
        url = clean_url(value, self.base)
        if url and (explicit or MEDIA.search(url)) and len(self.media) < MAX_CANDIDATES:
            self.media.append((url, str(title or '')[:200]))

    def add_frame(self, value, label=''):
        url = clean_url(value, self.base)
        if url and len(self.frames) < MAX_PAGES * 2 and (known_embed(url) or
                re.search(r'player|embed|video', urlsplit(url).path + ' ' + label, re.I)):
            self.frames.append((url, str(label or '')[:200]))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title': self.in_title = True
        if tag == 'video':
            self.has_video = True
            self.in_video += 1
            self.add_media(attrs.get('src'), attrs.get('title'), explicit=True)
            self.add_media(attrs.get('data-src'), attrs.get('title'), explicit=True)
        elif tag == 'source' and (self.in_video or str(attrs.get('type', '')).startswith('video/')):
            self.add_media(attrs.get('src') or attrs.get('data-src'), attrs.get('label'), explicit=True)
        elif tag == 'iframe':
            self.add_frame(attrs.get('src') or attrs.get('data-src'), attrs.get('title') or attrs.get('id', ''))
        elif tag == 'meta' and attrs.get('property', attrs.get('name', '')).lower() in (
                'og:video', 'og:video:url', 'og:video:secure_url', 'twitter:player:stream'):
            self.add_media(attrs.get('content'))
        elif tag == 'script':
            self.script = {'type': attrs.get('type', ''), 'src': attrs.get('src', ''), 'text': ''}

    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
        if tag == 'video': self.in_video = max(0, self.in_video - 1)
        if tag == 'script' and self.script is not None:
            self.scripts.append(self.script); self.script = None

    def handle_data(self, data):
        if self.in_title: self.title += data[:500]
        if self.script is not None: self.script['text'] += data

    def parse_scripts(self):
        for script in self.scripts:
            text = script['text']
            if script['type'] in ('application/ld+json', 'application/json'):
                try:
                    data = json.loads(text)
                except (ValueError, RecursionError):
                    continue
                self.json_sources(data, depth=0, video_object=False)
            # Read literal configuration only, never evaluate JavaScript or fetch script code.
            if any(re.search(pattern, text + ' ' + script['src'], re.I) for pattern in PLAYER_NAMES.values()):
                for match in re.finditer(r'''(?:["']?(?:file|src|source|hls|dash|contentUrl)["']?\s*:\s*|\.loadSource\s*\(\s*|\.load\s*\(\s*)["']([^"'\r\n]+)["']''', text, re.I):
                    self.add_media(match[1])

    def json_sources(self, data, depth, video_object):
        if depth > 12: return
        if isinstance(data, list):
            for value in data[:100]: self.json_sources(value, depth + 1, video_object)
        elif isinstance(data, dict):
            video_object = video_object or data.get('@type') == 'VideoObject'
            title = data.get('name') or data.get('label') or ''
            for key, value in list(data.items())[:100]:
                if key in ('contentUrl', 'file', 'src', 'source', 'hls', 'dash'):
                    self.add_media(value, title, explicit=video_object and key == 'contentUrl')
                if key == 'embedUrl' and video_object: self.add_frame(value, title)
                if isinstance(value, (dict, list)): self.json_sources(value, depth + 1, video_object)


def fetch_html(url, referer=None, timeout=10):
    validate_url(url)
    with httpx.Client(proxy=PROXY_URL or None, trust_env=False, follow_redirects=True,
                      max_redirects=3, timeout=timeout) as client:
        with client.stream('GET', url, headers={'Referer': referer} if referer else {}) as response:
            response.raise_for_status()
            kind = response.headers.get('content-type', '').lower()
            if kind and not any(t in kind for t in ('text/html', 'application/xhtml+xml')):
                raise MediaProblem('Ссылка не вернула HTML-страницу для поиска встроенного плеера.')
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_BYTES:
                    raise MediaProblem('Страница слишком велика для безопасного поиска плеера (лимит 2 МБ).')
            return chunks.decode(response.encoding or 'utf-8', errors='replace'), str(response.url)


def discover_embedded(url):
    queue = [(url, None, 0)]
    visited = set(); sources = {}; players = set(); root_title = 'Встроенные видео'
    has_blob = False; failed_frames = 0
    while queue and len(visited) < MAX_PAGES:
        page_url, referer, depth = queue.pop(0)
        if page_url in visited: continue
        visited.add(page_url)
        try:
            text, base = fetch_html(page_url, referer)
        except (httpx.HTTPError, MediaProblem, ValueError):
            if depth == 0: raise
            failed_frames += 1
            continue
        visited.add(base)
        parser = PlayerHTML(base); parser.feed(text); parser.parse_scripts()
        if depth == 0 and parser.title.strip(): root_title = parser.title.strip()[:200]
        has_blob |= parser.has_blob
        if parser.has_video: players.add('HTML5 video')
        for name, pattern in PLAYER_NAMES.items():
            if re.search(pattern, text, re.I): players.add(name)
        candidates = [(u, title, 'embedded_media') for u, title in parser.media]
        candidates += [(u, label, 'embedded_page') for u, label in parser.frames if known_embed(u)]
        for media_url, title, kind in candidates:
            identity = media_identity(base) + '|' + media_identity(media_url)
            key = hashlib.sha256(identity.encode()).hexdigest()[:24]
            if key in sources or len(sources) >= MAX_CANDIDATES: continue
            number = str(len(sources) + 1)
            extension = MEDIA.search(media_url)
            label = title or f'Вариант {number}' + (f' · {extension[1].upper()}' if extension else ' · встроенный плеер')
            sources[key] = {'kind': kind, 'url': media_url, 'referer': base, 'root_url': url,
                            'candidate_id': key, 'title': root_title + ' — ' + label,
                            'label': label, 'number': number}
        if depth < MAX_DEPTH:
            queue.extend((frame, base, depth + 1) for frame, _ in parser.frames if frame not in visited)
    if not sources:
        if has_blob:
            detail = 'Плеер использует blob:-адрес, существующий только внутри браузера. Исходный HTTP-поток в HTML не найден.'
        elif players:
            detail = 'Плеер распознан (' + ', '.join(sorted(players)) + '), но не публикует адрес видео в HTML. Нужен отдельный обработчик его API или JavaScript.'
        else:
            detail = 'В HTML не найдены публичные видеоисточники или поддерживаемые встроенные плееры.'
        if failed_frames: detail += ' Часть вложенных страниц недоступна с сервера.'
        raise MediaProblem(detail)
    numbered = {source['number']: source for source in sources.values()}
    return {'title': root_title, 'kind': 'embedded', 'note':
            'Найдены возможные источники, а не подтверждённые серии. Они могут быть разными качествами или разными видео. Выберите нужный вариант; доступность проверим перед скачиванием.',
            'groups': [{'id': 'embedded', 'label': 'Обнаруженные источники', 'episodes': list(numbered),
                        'available': list(numbered), 'players': sorted(players) or ['Встроенный плеер'],
                        'sources': numbered}],
            'items': [{'number': n, 'title': s['label']} for n, s in numbered.items()]}


def resolve_embedded(selection):
    catalog = discover_embedded(selection['root_url'])
    matches = [source for group in catalog['groups'] for source in group['sources'].values()
               if source['candidate_id'] == selection['candidate_id']]
    if len(matches) != 1:
        raise MediaProblem('Страница изменила список видео. Вставьте исходную ссылку заново и выберите источник повторно.')
    return matches[0]


def allow_discovery(exc):
    # Do not reinterpret authentication, DRM, HTTP blocking or missing videos as HTML discovery.
    text = str(exc).lower()
    if any(token in text for token in ('drm', '403', '401', '429', 'captcha', 'sign in',
                                       'login required', 'private video', 'not available in your country')):
        return False
    return 'unsupported url' in text or 'no suitable extractor' in text or (
        '[generic]' in text and ('no video formats found' in text or 'no video found' in text))

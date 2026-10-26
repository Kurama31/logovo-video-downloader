"""Best-effort player fingerprints, independent of download support and never executing JS."""
import re
from urllib.parse import urlsplit

import httpx

from app.embedded import PLAYER_NAMES, PlayerHTML, fetch_html
from app.plans import MediaProblem
from app.provider_adapters import provider_name
from app.security import validate_url


def blank_detection(note='Название плеера не удалось определить по открытым данным.'):
    return {'status': 'unknown', 'players': [], 'libraries': [], 'streams': [], 'note': note}


def player_url_label(url):
    provider = provider_name(url)
    if provider: return provider
    parts = urlsplit(url); host = parts.hostname or ''; path = parts.path
    if host == 'ru.yummyani.me' and path == '/iframeCVH.html': return 'CVH'
    if ((host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com')
         and re.match(r'^/(?:watch|shorts/|embed/)', path)) or (host == 'youtu.be' and path.strip('/'))):
        return 'YouTube'
    if host in ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com') and re.match(r'^/(?:video/)?\d+', path):
        return 'Vimeo'
    if host in ('vk.com', 'vkvideo.ru') and path.startswith('/video'): return 'VK Video'
    return None


def scan_player_html(text, base):
    parser = PlayerHTML(base)
    parser.feed(text); parser.parse_scripts()
    # Look at actual scripts, not prose that merely lists the names of player products.
    code = '\n'.join(script['src'] + '\n' + script['text'] for script in parser.scripts)
    players = {name for name, pattern in PLAYER_NAMES.items()
               if name not in ('hls.js', 'dash.js') and re.search(pattern, code, re.I)}
    libraries = set()
    if re.search(r'\bhls(?:\.min)?\.js\b|\bhlsjs\b|\bnew\s+Hls\s*\(|\bHls\.isSupported\b', code, re.I):
        libraries.add('hls.js')
    if re.search(r'\bdashjs\b|\bdash(?:\.all)?(?:\.min)?\.js\b', code, re.I):
        libraries.add('dash.js')
    if parser.has_video and not players:
        players.add('HTML5 video')
    for frame, _ in parser.frames:
        provider = player_url_label(frame)
        if provider: players.add(provider)
    streams = set()
    for url, _ in parser.media:
        path = urlsplit(url).path.lower()
        if path.endswith('.m3u8'): streams.add('HLS')
        elif path.endswith('.mpd'): streams.add('DASH')
        elif path.endswith(('.mp4', '.webm', '.mov', '.m4v', '.ogv')): streams.add('HTTP-файл')
    return players, libraries, streams, parser.frames


def detect_player(url):
    validate_url(url)
    result = blank_detection()
    provider = player_url_label(url)
    if provider:
        return {**result, 'status': 'detected', 'players': [provider],
                'note': 'Определено по адресу плеера. Это не подтверждение доступности скачивания.'}
    parsed = urlsplit(url)
    path = parsed.path.lower()
    if path.endswith(('.m3u8', '.mpd', '.mp4', '.webm', '.mov', '.m4v', '.ogv')):
        stream = 'HLS' if path.endswith('.m3u8') else 'DASH' if path.endswith('.mpd') else 'HTTP-файл'
        return {**result, 'status': 'direct', 'streams': [stream],
                'note': 'Прямая ссылка на файл или манифест: название веб-плеера из неё не следует.'}
    # A catalogue's actual player names come from its episode metadata, not its loading placeholder.
    if parsed.hostname in ('old.yummy-ani.me', 'yummy-ani.me', 'ru.yummyani.me') and '/catalog/item/' in path:
        return blank_detection('Список плееров каталога появится после получения серий.')
    players, libraries, streams = set(), set(), set()
    try:
        text, base = fetch_html(url, timeout=4)
        found, libs, methods, frames = scan_player_html(text, base)
        players.update(found); libraries.update(libs); streams.update(methods)
        # One nested page at most. Keep this diagnostic bounded and cheaper than extraction.
        frame = next((frame for frame, _ in frames if frame != base and not player_url_label(frame)), None)
        if frame:
            try:
                nested, nested_base = fetch_html(frame, base, timeout=4)
                found, libs, methods, _ = scan_player_html(nested, nested_base)
                players.update(found); libraries.update(libs); streams.update(methods)
            except (httpx.HTTPError, MediaProblem, ValueError):
                pass  # Preserve already detected outer-page evidence.
    except (httpx.HTTPError, MediaProblem, ValueError):
        return blank_detection('Не удалось прочитать открытый HTML для определения плеера. Это не меняет результат проверки скачивания.')
    return {**result, 'status': 'detected' if players else 'unknown', 'players': sorted(players),
            'libraries': sorted(libraries), 'streams': sorted(streams),
            'note': 'По признакам в HTML/скриптах; версия и доступность скачивания не подтверждаются.'}


def enrich_detection(detection, record=None, catalog=None):
    result = {**detection}
    if catalog:
        names = {name.removeprefix('Плеер ') for group in catalog['groups'] for name in group['players']}
        result['players'] = sorted(set(result['players']) | names)
        result['note'] = 'Плееры перечислены в каталоге. Фактический поток проверяется после выбора серии и озвучки.'
    if record:
        streams = set(result['streams'])
        for stream in record.get('streams', []):
            if stream.get('method'):
                streams.add(stream['method'])
        result['streams'] = sorted(streams)
    if result['players']: result['status'] = 'detected'
    return result


def public_detection(value):
    if not isinstance(value, dict): return None
    # Only descriptive labels, never manifests, cookies or signed URLs.
    result = {key: [item for item in value.get(key, []) if isinstance(item, str)
                   and re.fullmatch(r'[\w .()+/-]{1,80}', item)][:12]
              for key in ('players', 'libraries', 'streams')}
    result['status'] = value.get('status') if value.get('status') in ('unknown', 'detected', 'direct') else 'unknown'
    result['note'] = str(value.get('note') or '')[:300]
    return result

"""Provider-specific metadata adapters. No playback, remote JS execution or media decryption."""
import base64
import hashlib
import json
import re
import string
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import PROXY_URL
from app.embedded import PlayerHTML
from app.plans import MediaProblem, manifest_text
from app.security import validate_url


def provider_name(url):
    host = urlsplit(url).hostname or ''
    if host == 'kodikplayer.com': return 'Kodik'
    if host in ('alloha.yani.tv', 'alloha.tv'): return 'Alloha'
    return None


def response_text(response):
    response.raise_for_status()
    data = bytearray()
    for block in response.iter_bytes():
        data.extend(block)
        if len(data) > 2 * 1024**2:
            raise MediaProblem('Ответ плеера превышает безопасный лимит метаданных.')
    return data.decode('utf-8', errors='replace')


def decode_kodik_url(value, base):
    # Kodik's JS rotates the base64 URL string. This is URL encoding, not video/DRM decryption.
    if not isinstance(value, str) or len(value) > 8192:
        raise MediaProblem('Kodik вернул некорректный адрес потока.')
    if '//' not in value:
        letters = string.ascii_lowercase
        upper = string.ascii_uppercase
        table = str.maketrans(letters + upper, letters[18:] + letters[:18] + upper[18:] + upper[:18])
        try:
            raw = value.translate(table)
            value = base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True).decode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise MediaProblem('Kodik изменил кодирование адресов. Эта версия ответа пока не поддерживается.') from exc
    return validate_url(urljoin(base, value))


def kodik_sources(url):
    validate_url(url)
    if provider_name(url) != 'Kodik': raise MediaProblem('Неизвестный домен Kodik.')
    with httpx.Client(proxy=PROXY_URL or None, trust_env=False, timeout=15,
                      follow_redirects=True, max_redirects=3) as client:
        with client.stream('GET', url, headers={'Referer': 'https://old.yummy-ani.me/'}) as response:
            page = response_text(response)
            base = str(response.url)
        if provider_name(base) != 'Kodik':
            raise MediaProblem('Kodik перенаправил на неподдерживаемый домен. Нужна проверка новой версии плеера.')
        params = re.search(r"\bvar\s+urlParams\s*=\s*'([^']+)'", page)
        if not params:
            raise MediaProblem('Kodik не предоставил параметры API. Возможно, требуется проверка доступа или изменился плеер.')
        try:
            data = json.loads(params[1])
            body = {key: str(data[key]) for key in ('d', 'd_sign', 'pd', 'pd_sign', 'ref', 'ref_sign')}
        except (ValueError, KeyError, TypeError) as exc:
            raise MediaProblem('Kodik изменил формат параметров API.') from exc
        for key in ('type', 'hash', 'id', 'link', 'secret', 'uid'):
            match = re.search(r"\bvInfo\." + key + r"\s*=\s*'([^']*)'", page)
            if match: body[key] = match[1]
        if not all(key in body for key in ('type', 'hash', 'id')):
            raise MediaProblem('Kodik не предоставил ID выбранной серии.')
        endpoint = urljoin(base, '/ftor')
        with client.stream('POST', endpoint, data=body,
                           headers={'Referer': base, 'Origin': 'https://kodikplayer.com',
                                    'X-Requested-With': 'XMLHttpRequest'}) as response:
            if response.status_code >= 500:
                raise MediaProblem(f'Kodik: API выбранной серии вернул HTTP {response.status_code}. Поток не получен; повторите позже или выберите другой плеер той же озвучки.')
            text = response_text(response)
        try: result = json.loads(text)
        except ValueError as exc:
            raise MediaProblem('Kodik вернул не JSON с видеопотоками. Возможна проверка доступа.') from exc
    if not isinstance(result, dict): raise MediaProblem('Kodik вернул неожиданный ответ API.')
    sources = []
    links = result.get('links') or {}
    if not isinstance(links, dict): raise MediaProblem('Kodik изменил формат списка качеств.')
    for quality, items in list(links.items())[:30]:
        if not isinstance(items, list): continue
        for item in items[:5]:
            if not isinstance(item, dict) or not item.get('src'): continue
            sources.append({'url': decode_kodik_url(item['src'], base),
                            'height': int(quality) if str(quality).isdigit() else None,
                            'headers': {'Referer': base}})
            break  # CDN alternatives of the same quality are not separate videos.
    if not sources and isinstance(result.get('link'), str):
        sources.append({'url': decode_kodik_url(result['link'], base), 'height': None,
                        'headers': {'Referer': base}})
    if not sources:
        raise MediaProblem('Kodik ответил, но не предоставил видео. Доступность страницы не подтверждает доступность потока.')
    return sources


def alloha_sources(url):
    validate_url(url)
    if provider_name(url) != 'Alloha': raise MediaProblem('Неизвестный домен Alloha.')
    text, base = manifest_text(url, {'Referer': 'https://old.yummy-ani.me/'})
    if provider_name(base) != 'Alloha': raise MediaProblem('Alloha перенаправил на неподдерживаемый домен.')
    parser = PlayerHTML(base); parser.feed(text); parser.parse_scripts()
    sources = [{'url': media, 'height': None, 'headers': {'Referer': base}}
               for media, _ in parser.media]
    if not sources:
        raise MediaProblem('Alloha: открытые MP4/HLS/DASH-адреса отсутствуют. Эта версия выдаёт поток через динамический JavaScript/API; она пока не поддерживается. Просмотр до конца не требуется, но нужен другой адаптер этой версии плеера.')
    return sources


def provider_info(url, target, extractor, title=None):
    provider = provider_name(url)
    sources = kodik_sources(url) if provider == 'Kodik' else alloha_sources(url)
    # Multiple unrelated sources must be selected explicitly, not substituted silently.
    if provider == 'Alloha' and len({source['url'] for source in sources}) > 1:
        raise MediaProblem('Alloha опубликовал несколько разных источников. Нужен явный выбор, чтобы не подменить видео или озвучку.')
    known = sorted({s['height'] for s in sources if s['height']})
    height = (max(known) if not target else next((n for n in known if n >= target), max(known))) if known else None
    source = next((s for s in sources if s['height'] == height), sources[0])
    if re.search(r'\.(?:m3u8|mpd)(?:$|[?#])', source['url'], re.I):
        info = extractor(source['url'], source['headers'])
        if not info or info.get('entries') is not None:
            raise MediaProblem(f'{provider} не предоставил один поддерживаемый видеопоток.')
    else:
        suffix = urlsplit(source['url']).path.rsplit('.', 1)[-1].lower()
        ext = suffix if suffix in ('mp4', 'webm', 'mov', 'm4v', 'ogv') else 'mp4'
        info = {'formats': [{'url': source['url'], 'height': source['height'],
                 'format_id': str(source['height'] or 'source'), 'ext': ext, 'protocol': urlsplit(source['url']).scheme,
                 'vcodec': 'unknown', 'acodec': 'unknown', 'http_headers': source['headers']}]}
    info['id'] = provider.lower() + ':' + hashlib.sha256(url.encode()).hexdigest()[:24]
    info['extractor_key'] = provider
    info['title'] = title or f'Видео {provider}'
    info['http_headers'] = source['headers']
    return info

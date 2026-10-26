"""Build browser download plans. Only manifests/metadata are buffered on the server."""
import hashlib
import json
import re
from urllib.parse import urljoin

import httpx

from app.config import PROXY_URL
from app.security import validate_url


class MediaProblem(ValueError):
    pass


def explain_error(exc):
    if isinstance(exc, MediaProblem):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        messages = {400: 'Источник отклонил запрос потока (HTTP 400). Возможно, истекла ссылка или требуется контекст плеера.',
                    401: 'Источник требует авторизацию (HTTP 401).',
                    403: 'Источник запретил доступ с сервера (HTTP 403). Возможны ограничения IP или доступа.',
                    404: 'Источник не нашёл страницу или видеопоток (HTTP 404).',
                    429: 'Источник ограничил частоту запросов (HTTP 429). Повторите позже.'}
        return messages.get(code, f'Источник вернул HTTP {code} при получении метаданных или потока.')
    text = str(exc).lower()
    checks = [
        (('confirm you’re not a bot', "confirm you're not a bot", 'bot detection'),
         'Сайт требует проверку «Я не робот». С IP сервера получить видео сейчас нельзя.'),
        (('sign in', 'login required', 'private video', 'login_required', 'authentication'),
         'Сайт требует вход в аккаунт или видео закрыто. Авторизация в этом приложении не поддерживается.'),
        (('not available in your country', 'geo restricted', 'geo-restricted'),
         'Видео недоступно в регионе сервера: географическое ограничение источника.'),
        (('drm',), 'Видео защищено DRM. Получение и сборка такого потока не поддерживаются.'),
        (('unsupported url', 'no suitable extractor'), 'Этот сайт или тип ссылки не поддерживается.'),
        (('http error 403', '403 forbidden'), 'Источник запретил доступ к видео (HTTP 403). Возможны ограничения IP или доступа.'),
        (('http error 400', '400 bad request'), 'Источник отклонил запрос видеопотока (HTTP 400). Возможна устаревшая ссылка или несовместимый контекст запроса.'),
        (('http error 401', '401 unauthorized'), 'Источник требует авторизацию (HTTP 401).'),
        (('http error 451',), 'Источник ограничил доступ по юридическим причинам (HTTP 451).'),
        (('http error 429', 'too many requests'), 'Источник ограничил частоту запросов (HTTP 429). Попробуйте позже.'),
        (('404', 'not found', 'video unavailable', 'removed'), 'Видео удалено, недоступно или ссылка больше не действует.'),
        (('timed out', 'timeout', 'connection'), 'Не удалось связаться с источником. Проверьте ссылку или повторите позже.'),
    ]
    for patterns, message in checks:
        if any(pattern in text for pattern in patterns):
            return message
    return 'не удалось получить видео по вашей ссылке'


def manifest_text(url, headers):
    validate_url(url)
    with httpx.Client(proxy=PROXY_URL or None, trust_env=False, follow_redirects=True,
                      timeout=20, max_redirects=5) as client:
        with client.stream('GET', url, headers=headers) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 4 * 1024**2:
                    raise MediaProblem('Плейлист источника слишком велик для безопасного разбора.')
            return data.decode('utf-8-sig'), str(response.url)


def hls_parts(url, headers):
    text, base = manifest_text(url, headers)
    if '#EXTM3U' not in text or '#EXT-X-STREAM-INF' in text:
        raise MediaProblem('Источник не предоставил конечный HLS-поток выбранного качества.')
    if '#EXT-X-ENDLIST' not in text:
        raise MediaProblem('Незавершённые потоки и прямые эфиры пока не поддерживаются.')
    if '#EXT-X-DISCONTINUITY' in map(str.strip, text.splitlines()):
        raise MediaProblem('HLS с изменением временной шкалы пока нельзя безопасно собрать в браузере.')
    result = []
    byte_range = None
    offsets = {}
    for line in map(str.strip, text.splitlines()):
        if line.startswith('#EXT-X-KEY:') and not re.search(r'(?:^|,)METHOD=NONE(?:,|$)', line.split(':', 1)[1]):
            raise MediaProblem('Зашифрованный HLS-поток: локальная сборка этого типа шифрования пока не поддерживается.')
        if line.startswith('#EXT-X-MAP:'):
            match = re.search(r'URI="([^"]+)"', line)
            if not match:
                raise MediaProblem('Некорректный заголовок HLS-потока.')
            part = {'url': urljoin(base, match[1])}
            match_range = re.search(r'BYTERANGE="(\d+)@(\d+)"', line)
            if 'BYTERANGE=' in line and not match_range:
                raise MediaProblem('Этот вариант диапазонов HLS пока не поддерживается.')
            if match_range:
                length, start = map(int, match_range.groups())
                part.update(start=start, size=length)
            if result:
                raise MediaProblem('Поток меняет заголовок сегментов; такой HLS пока не поддерживается.')
            result.append(part)
        elif line.startswith('#EXT-X-BYTERANGE:'):
            byte_range = line.split(':', 1)[1]
        elif line and not line.startswith('#'):
            part = {'url': urljoin(base, line)}
            if byte_range:
                spec = byte_range.split('@')
                length = int(spec[0])
                start = int(spec[1]) if len(spec) == 2 else offsets.get(part['url'], 0)
                part.update(start=start, size=length)
                offsets[part['url']] = start + length
                byte_range = None
            result.append(part)
    if not result or len(result) > 10000:
        raise MediaProblem('Плейлист пуст или содержит слишком много сегментов.')
    for part in result:
        validate_url(part['url'])
    return result


def stream_plan(fmt, headers):
    protocol = fmt.get('protocol', 'https')
    if protocol in ('http', 'https'):
        parts = [{'url': fmt['url'], 'size': fmt.get('filesize')}]
    elif protocol in ('m3u8', 'm3u8_native'):
        parts = hls_parts(fmt['url'], headers)
    elif protocol == 'http_dash_segments':
        parts = []
        for fragment in fmt.get('fragments') or []:
            url = fragment.get('url') or urljoin(fmt.get('fragment_base_url') or fmt['url'], fragment.get('path', ''))
            part = {'url': url}
            if fragment.get('range'):
                raise MediaProblem('Этот вариант диапазонов DASH пока не поддерживается.')
            parts.append(part)
        if not parts:
            raise MediaProblem('Источник не предоставил список DASH-сегментов.')
    else:
        raise MediaProblem('Протокол потока не поддерживается для сборки на устройстве.')
    for part in parts:
        validate_url(part['url'])
    method = 'HLS' if protocol in ('m3u8', 'm3u8_native') else 'DASH' if protocol == 'http_dash_segments' else 'HTTP-файл'
    return {'headers': headers, 'parts': parts, 'ext': fmt.get('ext'), 'method': method,
            'video': fmt.get('vcodec') != 'none', 'audio': fmt.get('acodec') not in (None, 'none')}


def plan_signature(record):
    # Signed CDN URLs may expire. Compare media identities, not short-lived tokens.
    data = {key: record.get(key) for key in ('source_id', 'extractor', 'ids', 'fingerprints')}
    data['parts'] = [[(p.get('size'), p.get('start')) for p in s.get('parts', [])]
                     for s in record['streams']]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

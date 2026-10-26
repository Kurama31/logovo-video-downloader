"""Explicit catalogue adapters; no browser execution or arbitrary iframe crawling."""
import hashlib
import html
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from app.plans import MediaProblem, manifest_text
from app.security import validate_url


class CatalogFound(Exception):
    def __init__(self, catalog):
        self.catalog = catalog
        super().__init__('Найден каталог видео: требуется выбор серий.')


def fetch_text(url, headers=None):
    # Bounded metadata only. All redirects also pass through the pinned egress proxy.
    return manifest_text(url, headers or {})[0]


def fetch_json(url, headers=None):
    try:
        data = json.loads(fetch_text(url, headers))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MediaProblem('Плеер вернул некорректные метаданные вместо списка видео.') from exc
    if not isinstance(data, dict):
        raise MediaProblem('Плеер вернул неожиданный формат метаданных.')
    return data


def yummy_url(url):
    parsed = urlsplit(url)
    return parsed.hostname in ('old.yummy-ani.me', 'yummy-ani.me', 'www.yummy-ani.me',
                               'ru.yummyani.me') and re.fullmatch(r'/catalog/item/[\w-]+/?', parsed.path)


def sort_episode(number):
    return (0, float(number)) if re.fullmatch(r'\d+(?:\.\d+)?', number) else (1, number)


def yummy_catalog(url):
    page = fetch_text(url)
    match = re.search(r'class=["\'][^"\']*rating-info[^"\']*["\'][^>]*data-id=["\'](\d+)', page)
    if not match:
        raise MediaProblem('Страница каталога распознана, но ID сериала не найден. Возможно, сайт изменился или требует проверку доступа.')
    anime_id = match[1]
    data = fetch_json(f'https://old.yummy-ani.me/api/anime/{anime_id}/videos')
    entries = data.get('response')
    if not isinstance(entries, list) or not entries:
        raise MediaProblem('Сайт распознан, но его каталог не предоставил ни одной серии.')
    title = re.search(r'<h1\b[^>]*>(.*?)</h1>', page, re.S)
    title = html.unescape(re.sub('<[^>]+>', '', title[1])).strip() if title else 'Сериал'
    groups = {}
    for entry in entries[:5000]:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get('number', '')).strip()
        voice = str(entry.get('data', {}).get('dubbing') or 'Исходная дорожка')[:200]
        if not number or len(number) > 30:
            continue
        key = hashlib.sha256(voice.encode()).hexdigest()[:20]
        group = groups.setdefault(key, {'id': key, 'label': voice, 'episodes': [],
                                        'available': [], 'players': [], 'sources': {}})
        if number not in group['episodes']:
            group['episodes'].append(number)
        player = str(entry.get('data', {}).get('player') or 'Неизвестный плеер')
        if player not in group['players']:
            group['players'].append(player)
        # Only this known adapter is enabled. Never silently substitute a different voice.
        frame = urlsplit(urljoin(url, entry.get('iframe_url') or ''))
        query = parse_qs(frame.query)
        if ('CVH' in player and frame.hostname == 'ru.yummyani.me'
                and query.get('anime_id', [''])[0].isdigit() and query.get('dubbing_code')):
            if number not in group['available']:
                group['available'].append(number)
            group['sources'][number] = {'kind': 'yummy_cvh', 'anime_id': query['anime_id'][0],
                                         'voice': query['dubbing_code'][0], 'episode': number}
        elif frame.hostname in ('kodikplayer.com', 'alloha.yani.tv', 'alloha.tv'):
            provider = 'Kodik' if frame.hostname == 'kodikplayer.com' else 'Alloha'
            previous = group['sources'].get(number)
            priority = 2 if provider == 'Kodik' else 1
            old_priority = 3 if previous and previous['kind'] == 'yummy_cvh' else (
                2 if previous and previous.get('provider') == 'Kodik' else 1 if previous else 0)
            if priority > old_priority:
                group['sources'][number] = {'kind': 'provider', 'provider': provider,
                                             'url': urljoin(url, entry['iframe_url']), 'episode': number}
            if number not in group['available']: group['available'].append(number)
    for group in groups.values():
        for key in ('episodes', 'available'):
            group[key].sort(key=sort_episode)
    if not groups:
        raise MediaProblem('На странице есть каталог, но список серий не удалось разобрать.')
    return {'title': title[:250], 'kind': 'yummy', 'groups': list(groups.values()),
            'note': 'CVH поддержан. Kodik: экспериментальный API-адаптер; возможны ошибки источника. Alloha: только открытые настройки, динамическая версия пока не поддерживается. Серии в списке ещё не означают доступность потока: проверяем перед скачиванием. При наличии CVH используем его для той же озвучки.'}


def public_catalog(catalog):
    result = {**{key: catalog[key] for key in ('title', 'kind', 'note')},
            'groups': [{key: group[key] for key in ('id', 'label', 'episodes', 'available', 'players')}
                       for group in catalog['groups']], 'containers': ['source', 'mp4', 'mkv'],
            'max_episodes': 100}
    if catalog.get('kind') == 'embedded':
        result['items'] = catalog.get('items', [])
    return result


def playlist_catalog(info):
    sources = {}
    for index, entry in enumerate(info.get('entries') or [], 1):
        if index > 100:
            raise MediaProblem('В плейлисте больше 100 видео. Используйте меньший список или ссылки на отдельные видео.')
        if not isinstance(entry, dict):
            continue
        url = entry.get('webpage_url') or entry.get('url')
        try:
            validate_url(url or '')
        except (ValueError, TypeError):
            continue
        sources[str(index)] = {'kind': 'url', 'url': url,
                               'title': str(entry.get('title') or f'Видео {index}')[:250]}
    if not sources:
        raise MediaProblem('Источник сообщил о плейлисте, но не предоставил ссылки на его видео.')
    return {'title': str(info.get('title') or 'Плейлист')[:250], 'kind': 'playlist',
            'note': 'Видео будут проверены и скачаны по очереди. Исходные звуковые дорожки не меняются.',
            'groups': [{'id': 'original', 'label': 'Исходные дорожки', 'episodes': list(sources),
                        'available': list(sources), 'players': ['Источник'], 'sources': sources}]}


def cvh_info(selection):
    headers = {'Referer': 'https://ru.yummyani.me/', 'Origin': 'https://ru.yummyani.me'}
    # API context and CDN media requests have different header requirements.
    # Sending the API Origin header to this CDN makes valid MP4 ranges fail HTTP 400.
    media_headers = {'Referer': 'https://ru.yummyani.me/'}
    anime_id, episode, voice = selection['anime_id'], selection['episode'], selection['voice']
    if not re.fullmatch(r'\d+', str(anime_id)) or not re.fullmatch(r'\d+', str(episode)):
        raise MediaProblem('CVH не поддерживает этот номер серии.')
    playlist = fetch_json('https://plapi.cdnvideohub.com/api/v1/player/sv/playlist'
                          f'?pub=745&aggr=mali&id={anime_id}&ep={episode}', headers)
    matches = [item for item in playlist.get('items') or []
               if str(item.get('episode')) == str(episode)
               and str(item.get('voiceStudio', '')).casefold() == voice.casefold()
               and item.get('season', 1) == 1]
    if not matches:
        raise MediaProblem(f'CVH не предоставил серию {episode} с выбранной озвучкой. Другая озвучка автоматически не подставляется.')
    video_id = str(matches[0].get('vkId') or '')
    if not video_id.isdigit():
        raise MediaProblem('Плеер не предоставил идентификатор видео: серия недоступна.')
    data = fetch_json(f'https://plapi.cdnvideohub.com/api/v1/player/sv/video/{video_id}', headers)
    sources = data.get('sources') or {}
    if not sources.get('hlsUrl'):
        raise MediaProblem('Серия найдена, но CVH не предоставил адрес потока. Возможны ограничения источника.')
    text, base = manifest_text(sources['hlsUrl'], media_headers)
    formats = []
    attrs = None
    direct_keys = {'mobile': 'mpegTinyUrl', 'lowest': 'mpegLowestUrl', 'low': 'mpegLowUrl',
                   'sd': 'mpegMediumUrl', 'hd': 'mpegHighUrl', 'full': 'mpegFullHdUrl',
                   'quad': 'mpegQhdUrl', 'quad2': 'mpeg2kUrl', 'ultra': 'mpeg4kUrl'}
    for line in map(str.strip, text.splitlines()):
        if line.startswith('#EXT-X-STREAM-INF:'):
            attrs = dict(re.findall(r'([\w-]+)=("[^"]*"|[^,]+)', line.split(':', 1)[1]))
        elif line and not line.startswith('#') and attrs:
            size = re.fullmatch(r'(\d+)x(\d+)', attrs.get('RESOLUTION', ''))
            quality = attrs.get('QUALITY', '').strip('"')
            direct = sources.get(direct_keys.get(quality, ''))
            formats.append({'format_id': quality or str(len(formats)), 'width': int(size[1]) if size else None,
                            'height': int(size[2]) if size else None, 'url': direct or urljoin(base, line),
                            'ext': 'mp4', 'protocol': 'https' if direct else 'm3u8_native',
                            'vcodec': 'unknown', 'acodec': 'unknown', 'http_headers': media_headers})
            attrs = None
    if not formats:
        raise MediaProblem('Плеер не предоставил поддерживаемые варианты качества видео.')
    formats.sort(key=lambda fmt: fmt.get('height') or 0)
    return {'id': f'cvh:{anime_id}:{voice}:{episode}', 'extractor_key': 'YummyCVH',
            'title': selection['title'], 'formats': formats, 'http_headers': media_headers}

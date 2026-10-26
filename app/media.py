import json

import yt_dlp

from app.config import MAX_FILE_BYTES, PROXY_URL
from app.security import validate_url
from app.plans import MediaProblem, stream_plan
from app.catalog import CatalogFound, cvh_info, playlist_catalog, yummy_catalog, yummy_url
from app.embedded import allow_discovery, discover_embedded, resolve_embedded
from app.provider_adapters import provider_info, provider_name


def resolution(fmt):
    # Match 1080p on vertical videos by their short edge too.
    width, height = fmt.get('width'), fmt.get('height')
    return min(width, height) if width and height else height


def choose_formats(formats, target):
    available = [f for f in formats if not f.get('has_drm') and f.get('vcodec') != 'none'
                 and f.get('url') and f.get('protocol', 'https') in
                 ('https', 'http', 'm3u8', 'm3u8_native', 'http_dash_segments')]
    if not available:
        raise MediaProblem('Доступное видео не найдено. Возможно, требуется вход или поток защищён DRM.')
    known = [f for f in available if resolution(f)]
    if known:
        heights = sorted({resolution(f) for f in known})
        height = max(heights) if not target else next((h for h in heights if h >= target), max(heights))
        available = [f for f in known if resolution(f) == height]
    video = available[-1]  # yt-dlp supplies formats sorted worst to best.
    if video.get('acodec') not in (None, 'none'):
        return [video]
    audio = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') not in (None, 'none')
             and not f.get('has_drm') and f.get('url')]
    if not audio:
        return [video]  # A genuinely silent source remains silent.
    compatible = [f for f in audio if f.get('ext') == ('m4a' if video.get('ext') == 'mp4' else 'webm')]
    return [video, (compatible or audio)[-1]]


def options():
    return {'quiet': True, 'no_warnings': True, 'noplaylist': True, 'socket_timeout': 20,
            'retries': 3, 'fragment_retries': 3, 'proxy': PROXY_URL or None,
            'cachedir': False, 'max_filesize': MAX_FILE_BYTES, 'restrictfilenames': True,
            'overwrites': False, 'continuedl': True, 'concurrent_fragment_downloads': 1,
            'remote_components': [], 'enable_file_urls': False, 'js_runtimes': {'node': {}}}


def inspect(url, target, progress=None, selection=None):
    validate_url(url)
    if progress:
        progress(25)  # URL validation completed: one of four analysis stages.
    if provider_name(url):
        def extract_provider(source_url, headers):
            with yt_dlp.YoutubeDL(options() | {'http_headers': headers}) as ydl:
                return ydl.extract_info(source_url, download=False)
        info = provider_info(url, target, extract_provider, (selection or {}).get('title'))
    elif selection and selection.get('kind') == 'yummy_cvh':
        info = cvh_info(selection)
    else:
        if yummy_url(url):
            raise CatalogFound(yummy_catalog(url))
        embedded = selection and selection.get('kind') in ('embedded_media', 'embedded_page')
        source = resolve_embedded(selection) if embedded else None
        opts = options() | {'extract_flat': 'in_playlist'}
        if source:
            opts['http_headers'] = {'Referer': source['referer']}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source['url'] if source else url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            if source:
                if allow_discovery(exc):
                    raise MediaProblem('Встроенный плеер найден, но его поток скрыт за JavaScript или API. Для него пока нет обработчика.') from exc
                raise
            if not allow_discovery(exc):
                raise
            raise CatalogFound(discover_embedded(url)) from exc
        if source and info:
            info['title'] = selection['title']
            if info.get('entries') is None:
                info['id'] = 'embedded:' + source['candidate_id']
                info['extractor_key'] = 'EmbeddedHTML'
    if progress:
        progress(50)  # Extractor returned source metadata.
    if not info:
        raise MediaProblem('Источник ответил без метаданных видео; наличие страницы не означает доступность её потока с сервера.')
    if info.get('_type') in ('playlist', 'multi_video') or info.get('entries') is not None:
        raise CatalogFound(playlist_catalog(info))
    if info.get('is_live') or info.get('live_status') == 'is_live':
        raise MediaProblem('Прямые трансляции пока не поддерживаются.')
    formats = info.get('formats') or [info]
    selected = choose_formats(formats, target)
    for fmt in selected:
        validate_url(fmt['url'])
    total = sum(f.get('filesize') or f.get('filesize_approx') or 0 for f in selected) or None
    if total and total > MAX_FILE_BYTES:
        raise MediaProblem('Видео превышает допустимый размер.')
    direct = len(selected) == 1 and selected[0].get('protocol', 'https') in ('http', 'https')
    ext = selected[0].get('ext', 'mp4')
    if len(selected) > 1:
        if not (ext == 'mp4' and selected[1].get('ext') in ('m4a', 'mp4')) and not (
                ext == 'webm' and selected[1].get('ext') == 'webm'):
            ext = 'mkv'
    container = (selection or {}).get('container', 'source')
    if container not in ('source', 'mp4', 'mkv'):
        raise MediaProblem('Этот контейнер не поддерживается.')
    remux = not direct or (container != 'source' and container != ext)
    if container != 'source':
        ext = container
    if progress:
        progress(75)  # Quality, tracks and estimated size checked; resolving segment plans next.
    record = {'source_id': info.get('id'), 'extractor': info.get('extractor_key'),
              'ids': [f.get('format_id') for f in selected],
              'fingerprints': [fingerprint(f) for f in selected],
              'streams': [stream_plan(f, f.get('http_headers') or info.get('http_headers') or {})
                          for f in selected], 'remux': remux}
    if selection:
        record['selection'] = selection
    return {'title': info.get('title') or 'Видео', 'selected': json.dumps(record),
            'actual_quality': resolution(selected[0]), 'extension': ext,
            'mode': 'browser', 'total': total}


def fingerprint(fmt):
    return {key: fmt.get(key) for key in ('format_id', 'width', 'height', 'vcodec', 'acodec', 'language')}


def refresh(job):
    selection = json.loads(job.selected or '{}').get('selection')
    fresh = inspect(job.url, job.quality, None, selection)
    old, new = json.loads(job.selected), json.loads(fresh['selected'])
    if any(old.get(k) != new.get(k) for k in ('source_id', 'extractor', 'ids', 'fingerprints')):
        raise ValueError('Источник изменил выбранные потоки. Создайте новую загрузку, чтобы не повредить файл.')
    if old.get('player_detection'):
        new['player_detection'] = old['player_detection']
        fresh['selected'] = json.dumps(new)
    return fresh

import json

import httpx
import pytest
import yt_dlp

from app.catalog import CatalogFound, public_catalog
from app.embedded import PlayerHTML, allow_discovery, discover_embedded, fetch_html, resolve_embedded
from app.media import inspect
from app.plans import MediaProblem

URL = 'https://example.com/watch'


def test_html5_and_signed_urls():
    parser = PlayerHTML(URL)
    parser.feed('<video src="/a.mp4?sig=x&amp;t=1"><source src="//cdn.example.com/a.webm"></video>'
                '<video src=""></video><audio><source src="sound.mp3"></audio>')
    assert parser.media == [('https://example.com/a.mp4?sig=x&t=1', ''),
                            ('https://cdn.example.com/a.webm', '')]


def test_json_and_player_literals_without_execution():
    parser = PlayerHTML(URL)
    parser.feed('''<script type="application/ld+json">{"@type":"VideoObject","name":"Demo","contentUrl":"/v.mp4"}</script>
      <script>jwplayer('p').setup({file:"https:\\/\\/cdn.example.com/h.m3u8?x=1\\u0026t=2"}); throw new Error('must never execute');</script>
      <script>const hls = new Hls(); hls.loadSource('/a.m3u8');</script>
      <script>fetch('/private-api');</script>''')
    parser.parse_scripts()
    assert [url for url, _ in parser.media] == [
        'https://example.com/v.mp4', 'https://cdn.example.com/h.m3u8?x=1&t=2', 'https://example.com/a.m3u8']


def test_nested_iframe_cycle_bounds_and_metadata(monkeypatch):
    seen = []
    pages = {URL: '<title>Demo</title><iframe src="/player/one"></iframe>',
             'https://example.com/player/one': '<video src="/sample.mp4"></video><iframe src="/watch" title="video"></iframe>'}
    def fetch(url, referer=None):
        seen.append((url, referer)); return pages[url], url
    monkeypatch.setattr('app.embedded.fetch_html', fetch)
    catalog = discover_embedded(URL)
    assert len(seen) == 2 and seen[1][1] == URL
    source = catalog['groups'][0]['sources']['1']
    assert source['referer'] == 'https://example.com/player/one'
    assert catalog['kind'] == 'embedded'
    public = public_catalog(catalog)
    assert 'url' not in json.dumps(public) and 'candidate_id' not in json.dumps(public)
    assert public['items'][0]['title'].startswith('Вариант 1')


def test_signed_url_is_rediscovered_by_identity(monkeypatch):
    token = ['old']
    monkeypatch.setattr('app.embedded.fetch_html', lambda *args: (
        f'<video src="/v.mp4?id=12&amp;sig={token[0]}"></video>', URL))
    source = discover_embedded(URL)['groups'][0]['sources']['1']
    token[0] = 'new'
    assert 'sig=new' in resolve_embedded(source)['url']
    monkeypatch.setattr('app.embedded.fetch_html', lambda *args: ('<video src="/different.mp4"></video>', URL))
    with pytest.raises(MediaProblem, match='Страница изменила'):
        resolve_embedded(source)


@pytest.mark.parametrize('text,message', [
    ('<video src="blob:local-id"></video>', 'blob:'),
    ('<script src="video.js"></script><div id="p"></div>', 'Плеер распознан'),
    ('<h1>Just a page</h1>', 'не найдены'),
])
def test_precise_static_discovery_limit(monkeypatch, text, message):
    monkeypatch.setattr('app.embedded.fetch_html', lambda *args: (text, URL))
    with pytest.raises(MediaProblem, match=message): discover_embedded(URL)


def test_discovery_not_for_access_restrictions():
    for text in ['HTTP Error 403', 'login required', 'DRM', 'Unsupported URL; CAPTCHA required']:
        assert not allow_discovery(text)
    assert allow_discovery('ERROR: Unsupported URL: https://example.com/watch')


def test_fetch_only_html_and_bounded_metadata(monkeypatch):
    actual = httpx.Client
    def client(kind, body):
        return actual(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={'Content-Type': kind}, content=body)))
    monkeypatch.setattr('app.embedded.httpx.Client', lambda **kwargs: client('video/mp4', b'video'))
    with pytest.raises(MediaProblem, match='HTML'): fetch_html(URL)
    monkeypatch.setattr('app.embedded.httpx.Client', lambda **kwargs: client('text/html', b'x'*(2*1024**2+1)))
    with pytest.raises(MediaProblem, match='2 МБ'): fetch_html(URL)


def test_fallback_and_embedded_source_plan(monkeypatch):
    monkeypatch.setattr('app.embedded.fetch_html', lambda *args: ('<video src="/v.mp4"></video>', URL))
    class Extractor:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, **kwargs):
            if url == URL: raise yt_dlp.utils.DownloadError('Unsupported URL')
            assert self.opts['http_headers']['Referer'] == URL
            return {'id': 'x', 'title': 'Source', 'formats': [{'url': url, 'ext': 'mp4', 'height': 720,
                    'vcodec': 'h264', 'acodec': 'aac', 'protocol': 'https', 'format_id': 'http',
                    'http_headers': self.opts['http_headers']}]}
    monkeypatch.setattr('app.media.yt_dlp.YoutubeDL', Extractor)
    with pytest.raises(CatalogFound) as found: inspect(URL, 1080)
    selection = found.value.catalog['groups'][0]['sources']['1']
    result = inspect(URL, 1080, None, selection)
    record = json.loads(result['selected'])
    assert result['actual_quality'] == 720 and result['mode'] == 'browser'
    assert record['streams'][0]['headers']['Referer'] == URL
    assert record['selection']['root_url'] == URL


def test_unsafe_sources_and_unrelated_frames_ignored():
    parser = PlayerHTML(URL)
    parser.feed('<video src="file:///secret"></video><source src="javascript:alert(1)" type="video/mp4">'
                '<iframe src="https://ads.example/banner"></iframe><video src="https://user:pass@example.com/a.mp4"></video>')
    assert parser.media == [] and parser.frames == []

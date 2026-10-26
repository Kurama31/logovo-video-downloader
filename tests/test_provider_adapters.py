import base64
import json
import string

import httpx
import pytest

from app.embedded import PlayerHTML
from app.plans import MediaProblem
from app.provider_adapters import alloha_sources, decode_kodik_url, kodik_sources, provider_info


def test_kodik_url_encoding_only():
    url = '//cdn.example.com/v.mp4?token=a'
    raw = base64.b64encode(url.encode()).decode()
    lower, upper = string.ascii_lowercase, string.ascii_uppercase
    encoded = raw.translate(str.maketrans(lower + upper, lower[-18:] + lower[:-18] + upper[-18:] + upper[:-18]))
    assert decode_kodik_url(encoded, 'https://kodikplayer.com/') == 'https:' + url
    with pytest.raises(MediaProblem): decode_kodik_url('not base64!', 'https://kodikplayer.com/')


@pytest.mark.parametrize('status', [200, 500])
def test_kodik_metadata_request_bounded(monkeypatch, status):
    actual = httpx.Client
    page = "var urlParams = '" + json.dumps({k: k for k in ('d','d_sign','pd','pd_sign','ref','ref_sign')}) + "';"
    page += "vInfo.type = 'seria'; vInfo.id = '123'; vInfo.hash = 'hash';"
    seen = []
    def serve(request):
        seen.append(request)
        if request.method == 'GET': return httpx.Response(200, text=page)
        return httpx.Response(status, json={'links': {'720': [{'src': '//cdn.example.com/720.mp4'}]}})
    monkeypatch.setattr('app.provider_adapters.httpx.Client', lambda **kwargs: actual(transport=httpx.MockTransport(serve)))
    if status == 500:
        with pytest.raises(MediaProblem, match='HTTP 500'): kodik_sources('https://kodikplayer.com/seria/123/hash/720p')
    else:
        sources = kodik_sources('https://kodikplayer.com/seria/123/hash/720p')
        assert sources[0]['height'] == 720
        assert seen[1].url.path == '/ftor' and seen[1].method == 'POST'
        assert b'id=123' in seen[1].content


def test_alloha_static_and_dynamic_versions(monkeypatch):
    monkeypatch.setattr('app.provider_adapters.manifest_text', lambda *args: ('<video src="https://cdn.example.com/v.mp4"></video>', 'https://alloha.yani.tv/'))
    assert alloha_sources('https://alloha.yani.tv/')[0]['url'] == 'https://cdn.example.com/v.mp4'
    monkeypatch.setattr('app.provider_adapters.manifest_text', lambda *args: ('<video src=""></video><script>obfuscated()</script>', 'https://alloha.yani.tv/'))
    with pytest.raises(MediaProblem, match='динамический'): alloha_sources('https://alloha.yani.tv/')


def test_quality_selection_and_no_video_download(monkeypatch):
    monkeypatch.setattr('app.provider_adapters.kodik_sources', lambda _: [
        {'url': f'https://cdn.example.com/{n}.mp4', 'height': n, 'headers': {'Referer': 'https://kodikplayer.com/'}}
        for n in [360, 720, 1080]])
    def no_extract(*args): raise AssertionError('Direct sources need no full-file request')
    info = provider_info('https://kodikplayer.com/seria/123/hash/720p', 480, no_extract)
    assert info['formats'][0]['height'] == 720 and info['extractor_key'] == 'Kodik'


def test_playerjs_labelled_sources():
    parser = PlayerHTML('https://example.com/player')
    parser.feed('''<script>new Playerjs({file:"[360p]https://cdn.example.com/360.mp4,[720p]https://cdn.example.com/720.mp4"})</script>''')
    parser.parse_scripts()
    assert [url for url, _ in parser.media] == ['https://cdn.example.com/360.mp4', 'https://cdn.example.com/720.mp4']

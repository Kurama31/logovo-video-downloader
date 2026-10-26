import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalog import CatalogFound
from app.main import app
from app.media import refresh
from app.db import Job
from app.player_detection import blank_detection, detect_player, public_detection, scan_player_html
from app.worker import execute

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}


@pytest.mark.parametrize('url,players,streams', [
    ('https://kodikplayer.com/seria/1/hash/720p', ['Kodik'], []),
    ('https://alloha.yani.tv/?episode=1', ['Alloha'], []),
    ('https://www.youtube.com/shorts/example', ['YouTube'], []),
    ('https://ru.yummyani.me/iframeCVH.html?anime_id=1', ['CVH'], []),
    ('https://example.com/video.m3u8?token=secret', [], ['HLS']),
    ('https://example.com/video.mpd', [], ['DASH']),
    ('https://example.com/video.mp4', [], ['HTTP-файл']),
])
def test_known_urls_do_not_download_pages(monkeypatch, url, players, streams):
    def no_fetch(*args, **kwargs): raise AssertionError('Must not fetch media for player detection')
    monkeypatch.setattr('app.player_detection.fetch_html', no_fetch)
    detection = detect_player(url)
    assert detection['players'] == players and detection['streams'] == streams
    assert 'secret' not in json.dumps(detection)


def test_separate_library_player_and_protocol():
    players, libraries, streams, _ = scan_player_html('''<script src="/video.js"></script>
        <script src="/hls.min.js"></script><video><source src="/master.m3u8"></video>''', 'https://example.com')
    assert players == {'Video.js'}
    assert libraries == {'hls.js'} and streams == {'HLS'}
    players, libraries, streams, _ = scan_player_html('<script>const hls=new Hls();</script>', 'https://example.com')
    assert players == set() and libraries == {'hls.js'} and not streams


def test_textual_mentions_are_not_player_fingerprints():
    players, libraries, _, _ = scan_player_html('<p>A comparison of Video.js and JW Player</p>', 'https://example.com')
    assert not players and not libraries


def test_nested_player_and_failure_are_best_effort(monkeypatch):
    seen = []
    def fetch(url, *args, **kwargs):
        seen.append(url)
        return ('<iframe src="/player/embed"></iframe>', url) if len(seen) == 1 else ('<script>jwplayer("p").setup({file:"/v.mp4"})</script>', url)
    monkeypatch.setattr('app.player_detection.fetch_html', fetch)
    assert detect_player('https://example.com/watch')['players'] == ['JW Player']
    assert len(seen) == 2
    def fail(*args, **kwargs): raise httpx.ConnectError('private network detail')
    monkeypatch.setattr('app.player_detection.fetch_html', fail)
    detection = detect_player('https://example.com/watch')
    assert detection['status'] == 'unknown' and 'private' not in json.dumps(detection)


def test_detection_survives_failed_extraction(monkeypatch):
    monkeypatch.setattr('app.worker.detect_player', lambda _: {**blank_detection(), 'players': ['Video.js'], 'status': 'detected'})
    def fail(*args): raise RuntimeError('download unavailable')
    monkeypatch.setattr('app.worker.inspect', fail)
    with TestClient(app) as client:
        job_id = client.post('/api/jobs', headers=HEADERS, json={'url':'https://example.com/watch'}).json()['id']
        execute(job_id)
        job = client.get('/api/jobs').json()[0]
    assert job['status'] == 'error'
    assert job['player_detection']['players'] == ['Video.js']


def test_catalog_lists_multiple_players(monkeypatch):
    def catalog(*args): raise CatalogFound({'title':'Series','kind':'yummy','note':'Catalog','groups':[
        {'id':'a','label':'A','players':['Плеер Kodik','Плеер CVH'], 'episodes':['1'],'available':['1'],'sources':{}}]})
    monkeypatch.setattr('app.worker.inspect', catalog)
    with TestClient(app) as client:
        job_id = client.post('/api/jobs', headers=HEADERS, json={'url':'https://example.com/catalog'}).json()['id']
        execute(job_id)
        job = client.get('/api/jobs').json()[0]
    assert job['player_detection']['players'] == ['CVH','Kodik']


def test_detection_is_preserved_on_signed_url_refresh(monkeypatch):
    record = {'source_id':'same','extractor':'Generic','ids':['1'],'fingerprints':[{}],
              'streams':[], 'player_detection':{**blank_detection(),'players':['JW Player']}}
    fresh = {key:value for key,value in record.items() if key != 'player_detection'}
    monkeypatch.setattr('app.media.inspect', lambda *args: {'selected':json.dumps(fresh)})
    result = refresh(Job(url='https://example.com/watch',quality=1080,selected=json.dumps(record)))
    assert json.loads(result['selected'])['player_detection']['players'] == ['JW Player']


def test_public_diagnostic_is_only_descriptive_labels():
    value = {**blank_detection(),'players':['Video.js','https://example.com/?token=secret'],
             'url':'https://example.com/?secret','streams':['HLS']}
    result = public_detection(value)
    assert result['players'] == ['Video.js'] and 'secret' not in json.dumps(result)

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalog import CatalogFound, cvh_info, playlist_catalog, public_catalog, yummy_catalog
from app.db import DB, Job
from app.main import app
from app.media import inspect, refresh
from app.plans import MediaProblem, explain_error
from app.worker import change, execute

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}
URL = 'https://old.yummy-ani.me/catalog/item/incident-kemono'


def catalog():
    return {'title': 'Сериал', 'kind': 'yummy', 'note': 'CVH', 'groups': [
        {'id': 'voice', 'label': 'Озвучка A', 'episodes': [str(n) for n in range(1, 13)],
         'available': [str(n) for n in range(1, 13)], 'players': ['CVH'],
         'sources': {str(n): {'kind': 'yummy_cvh', 'anime_id': '40908', 'voice': 'A', 'episode': str(n)}
                     for n in range(1, 13)}}]}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def parent(client):
    job_id = client.post('/api/jobs', headers=HEADERS, json={'url': URL}).json()['id']
    with DB.begin() as db:
        job = db.get(Job, job_id)
        job.status = 'selection'; job.phase = 'select'; job.mode = 'browser'
        job.selected = json.dumps({'catalog': catalog()})
    return job_id


def test_catalog_detection_precedes_generic_extractor(monkeypatch):
    monkeypatch.setattr('app.media.yummy_catalog', lambda _: catalog())
    with pytest.raises(CatalogFound):
        inspect(URL, 1080)


def test_yummy_grouping_and_public_metadata(monkeypatch):
    monkeypatch.setattr('app.catalog.fetch_text', lambda _: '<h1>Test &amp; series</h1><div class="rating-info" data-id="517">')
    entries = [{'number': str(n), 'data': {'dubbing': 'A', 'player': 'Плеер CVH'},
                'iframe_url': f'https://ru.yummyani.me/iframeCVH.html?anime_id=40908&dubbing_code=A&episode={n}'}
               for n in [10, 2, 1]]
    entries += [{'number': '1', 'data': {'dubbing': 'B', 'player': 'Kodik'},
                 'iframe_url': 'https://kodikplayer.com/video/1'}]
    monkeypatch.setattr('app.catalog.fetch_json', lambda _: {'response': entries})
    found = yummy_catalog(URL)
    assert found['title'] == 'Test & series'
    assert found['groups'][0]['available'] == ['1', '2', '10']
    assert found['groups'][1]['available'] == ['1']
    assert found['groups'][1]['sources']['1']['provider'] == 'Kodik'
    assert 'sources' not in json.dumps(public_catalog(found))
    assert 'iframe' not in json.dumps(public_catalog(found))


def test_worker_produces_selection_and_honors_cancel(client, monkeypatch):
    job_id = client.post('/api/jobs', headers=HEADERS, json={'url': URL}).json()['id']
    def found(*_): raise CatalogFound(catalog())
    monkeypatch.setattr('app.worker.inspect', found)
    execute(job_id)
    job = client.get('/api/jobs').json()[0]
    assert job['status'] == 'selection'
    assert len(job['catalog']['groups'][0]['available']) == 12
    client.post(f'/api/jobs/{job_id}/cancel', headers=HEADERS)
    change(job_id, status='selection')
    with DB() as db:
        assert db.get(Job, job_id).status == 'cancelled'


def test_batch_all_twelve_pending_not_server_downloads(client):
    job_id = parent(client)
    result = client.post(f'/api/catalogs/{job_id}/selection', headers=HEADERS,
                         json={'voice_id': 'voice', 'container': 'mkv'})
    assert result.status_code == 201
    jobs = result.json()['jobs']
    assert len(jobs) == 12
    assert all(job['status'] == 'pending' and job['mode'] == 'browser' and job['batch'] for job in jobs)
    with DB() as db:
        assert db.get(Job, job_id).status == 'cancelled'
        assert json.loads(db.get(Job, jobs[0]['id']).selected)['selection']['container'] == 'mkv'
    assert client.post(f'/api/jobs/{jobs[0]["id"]}/start', headers=HEADERS).json()['status'] == 'analyzing'
    assert client.post(f'/api/catalogs/{job_id}/selection', headers=HEADERS,
                       json={'voice_id': 'voice'}).status_code == 409


@pytest.mark.parametrize('body', [
    {'voice_id': 'missing'}, {'voice_id': 'voice', 'episodes': ['99']},
    {'voice_id': 'voice', 'episodes': []}, {'voice_id': 'voice', 'container': 'exe'},
])
def test_selection_rejects_invalid_input(client, body):
    job_id = parent(client)
    assert client.post(f'/api/catalogs/{job_id}/selection', headers=HEADERS, json=body).status_code == 422
    assert len(client.get('/api/jobs').json()) == 1


def test_selection_owner_and_duplicates(client):
    job_id = parent(client)
    with TestClient(app) as stranger:
        assert stranger.post(f'/api/catalogs/{job_id}/selection', headers=HEADERS,
                             json={'voice_id': 'voice'}).status_code == 404
    result = client.post(f'/api/catalogs/{job_id}/selection', headers=HEADERS,
                         json={'voice_id': 'voice', 'episodes': ['3', '1', '3']})
    assert len(result.json()['jobs']) == 2


def test_cvh_does_not_substitute_voice(monkeypatch):
    monkeypatch.setattr('app.catalog.fetch_json', lambda *args: {
        'items': [{'voiceStudio': 'Other', 'episode': 1, 'vkId': '123'}]})
    with pytest.raises(MediaProblem, match='Другая озвучка'):
        cvh_info({'anime_id': '40908', 'episode': '1', 'voice': 'A', 'title': 'Series'})


def test_cvh_quality_and_direct_stream(monkeypatch):
    def metadata(url, *_):
        if 'playlist?' in url:
            return {'items': [{'voiceStudio': 'A', 'episode': 1, 'vkId': '123'}]}
        return {'sources': {'hlsUrl': 'https://cdn.example/master.m3u8',
                            'mpegLowUrl': 'https://cdn.example/v.mp4'}}
    monkeypatch.setattr('app.catalog.fetch_json', metadata)
    monkeypatch.setattr('app.catalog.manifest_text', lambda *args: (
        '#EXTM3U\n#EXT-X-STREAM-INF:QUALITY=low,RESOLUTION=640x360\nv.m3u8\n', 'https://cdn.example/master.m3u8'))
    selection = {'kind': 'yummy_cvh', 'anime_id': '40908', 'episode': '1', 'voice': 'A',
                 'title': 'Series 1', 'container': 'mkv'}
    result = inspect(URL, 1080, None, selection)
    record = json.loads(result['selected'])
    assert result['actual_quality'] == 360 and result['extension'] == 'mkv'
    assert record['remux'] and record['selection'] == selection
    assert record['streams'][0]['parts'][0]['url'] == 'https://cdn.example/v.mp4'
    assert 'Origin' not in record['streams'][0]['headers']
    job = Job(url=URL, quality=1080, selected=result['selected'])
    assert json.loads(refresh(job)['selected'])['source_id'] == 'cvh:40908:A:1'


def test_playlist_and_specific_http_error():
    found = playlist_catalog({'entries': [{'url': 'https://example.com/v', 'title': 'One'}, {'url': 'bad'}]})
    assert found['groups'][0]['available'] == ['1']
    request = httpx.Request('GET', 'https://example.com')
    exc = httpx.HTTPStatusError('error', request=request, response=httpx.Response(403, request=request))
    assert 'HTTP 403' in explain_error(exc)
    assert 'HTTP 400' in explain_error('HTTP error 400')

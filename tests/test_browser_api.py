import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import DB, Job
from app.main import app
from app.plans import MediaProblem, explain_error, hls_parts, plan_signature
from app.storage import job_dir
from app.worker import cleanup

HEADERS={'Origin':'http://testserver','X-Requested-With':'DAV'}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def browser_job(client):
    response=client.post('/api/jobs',headers=HEADERS,json={'url':'https://example.com/video.mp4'})
    job_id=response.json()['id']
    record={'source_id':'test','extractor':'Generic','ids':['v','a'],'fingerprints':[{},{}],
            'remux':True,'streams':[{'headers':{},'ext':'mp4','parts':[{'url':'https://example.com/video.mp4','size':10}]}]}
    with DB.begin() as db:
        job=db.get(Job,job_id)
        job.mode='browser';job.status='awaiting';job.phase='download';job.selected=json.dumps(record)
        job.extension='mp4'
    return job_id,record


def test_start_does_not_queue_server_download(client):
    job_id,record=browser_job(client)
    response=client.post(f'/api/jobs/{job_id}/start',headers=HEADERS)
    assert response.json()['status']=='client_downloading'
    assert not job_dir(job_id).exists()
    client.post(f'/api/jobs/{job_id}/pause',headers=HEADERS).raise_for_status()
    assert client.post(f'/api/jobs/{job_id}/resume',headers=HEADERS).json()['status']=='client_downloading'


def test_delivery_diagnostic_is_owned_bounded_and_private(client, caplog):
    from app.browser_routes import delivery_log_limits
    job_id, _ = browser_job(client)
    endpoint = f'/api/jobs/{job_id}/client/delivery-log'
    payload = {'method': 'share', 'event': 'handed_off', 'visible': True,
               'worker_storage': True, 'activation': True}
    assert client.post(endpoint, headers=HEADERS, json=payload).status_code == 409
    with DB.begin() as db:
        db.get(Job, job_id).status = 'completed'
    with caplog.at_level('INFO', logger='uvicorn.error'):
        assert client.post(endpoint, headers=HEADERS, json=payload).json() == {'logged': True}
    assert 'client_delivery' in caplog.text
    assert 'https://example.com' not in caplog.text
    assert client.post(endpoint, headers=HEADERS, json={**payload, 'filename': 'secret.mp4'}).status_code == 422
    assert client.post(endpoint, headers=HEADERS, json={**payload, 'error_name': 'secret'}).status_code == 422
    with TestClient(app) as stranger:
        assert stranger.post(endpoint, headers=HEADERS, json=payload).status_code == 404
    delivery_log_limits[job_id] = (time.time(), 60)
    assert client.post(endpoint, headers=HEADERS, json=payload).status_code == 429
    with DB() as db:
        assert db.get(Job, job_id).status == 'completed'


def test_plan_has_no_source_secrets(client,monkeypatch):
    job_id,record=browser_job(client)
    monkeypatch.setattr('app.browser_routes.refresh',lambda _: {'selected':json.dumps(record)})
    response=client.get(f'/api/jobs/{job_id}/client/plan')
    assert response.status_code==200
    assert 'https://' not in response.text
    assert response.json()['remux'] is True


def test_client_progress_and_completion(client):
    job_id,_=browser_job(client)
    client.post(f'/api/jobs/{job_id}/start',headers=HEADERS)
    endpoint=f'/api/jobs/{job_id}/client/progress'
    assert client.post(endpoint,headers=HEADERS,json={'downloaded':5,'total':10,'phase':'download'}).status_code==200
    assert client.post(endpoint,headers=HEADERS,json={'downloaded':10,'total':10,'phase':'complete'}).json()['status']=='completed'
    assert not job_dir(job_id).exists()


def test_parts_proxy_and_ranges(client,monkeypatch):
    job_id,record=browser_job(client)
    client.post(f'/api/jobs/{job_id}/start',headers=HEADERS)
    actual=httpx.Client
    seen=[]
    def serve(request):
        seen.append(request)
        return httpx.Response(206,headers={'Content-Range':'bytes 5-9/10','Content-Length':'5','ETag':'"x"'},stream=httpx.ByteStream(b'56789'))
    monkeypatch.setattr('app.browser_routes.httpx.Client',lambda **_:actual(transport=httpx.MockTransport(serve)))
    response=client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0',headers={'Range':'bytes=5-','If-Range':'"x"'})
    assert response.status_code==206 and response.content==b'56789'
    assert seen[0].headers['range']=='bytes=5-9'
    assert client.get(f'/api/jobs/{job_id}/client/tracks/-1/parts/0').status_code==404
    client.post(f'/api/jobs/{job_id}/pause',headers=HEADERS)
    assert client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0').status_code==409
    assert not job_dir(job_id).exists()


def test_client_stale_pause(client):
    job_id,_=browser_job(client)
    with DB.begin() as db:
        job=db.get(Job,job_id);job.status='client_downloading';job.heartbeat=time.time()-100
    cleanup()
    with DB() as db:
        assert db.get(Job,job_id).status=='paused'


def mock_source(monkeypatch, serve):
    actual = httpx.Client
    monkeypatch.setattr('app.browser_routes.httpx.Client',
                        lambda **_: actual(transport=httpx.MockTransport(serve)))


def test_whole_file_without_validator_or_range(client, monkeypatch):
    job_id, _ = browser_job(client)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    def serve(request):
        assert 'range' not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(b'0123456789'))
    mock_source(monkeypatch, serve)
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0')
    assert response.status_code == 200 and response.content == b'0123456789'
    assert not job_dir(job_id).exists()


def test_partial_source_without_validator_is_forwarded(client, monkeypatch):
    job_id, _ = browser_job(client)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    def serve(request):
        assert request.headers['range'] == 'bytes=5-9'
        assert 'if-range' not in request.headers
        return httpx.Response(206, headers={'Content-Range': 'bytes 5-9/10'},
                              stream=httpx.ByteStream(b'56789'))
    mock_source(monkeypatch, serve)
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0', headers={'Range': 'bytes=5-'})
    assert response.status_code == 206 and response.content == b'56789'


@pytest.mark.parametrize('status', [400, 416])
def test_range_required_source_fallback(client, monkeypatch, status):
    job_id, _ = browser_job(client)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    seen = []
    def serve(request):
        seen.append(request)
        if 'range' not in request.headers:
            return httpx.Response(status, stream=httpx.ByteStream(b''))
        return httpx.Response(206, headers={'Content-Range': 'bytes 0-4/10'},
                              stream=httpx.ByteStream(b'01234'))
    mock_source(monkeypatch, serve)
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0')
    assert response.status_code == 206 and response.content == b'01234'
    assert len(seen) == 2 and seen[1].headers['range'] == 'bytes=0-9'


def test_authorization_error_is_not_retried_as_range(client, monkeypatch):
    job_id, _ = browser_job(client)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    seen = []
    def serve(request):
        seen.append(request)
        return httpx.Response(403, stream=httpx.ByteStream(b''))
    mock_source(monkeypatch, serve)
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0')
    assert response.status_code == 502 and len(seen) == 1


def test_ignored_range_forwards_full_file_for_client_restart(client, monkeypatch):
    job_id, _ = browser_job(client)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    mock_source(monkeypatch, lambda _: httpx.Response(200, stream=httpx.ByteStream(b'0123456789')))
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0',
                          headers={'Range': 'bytes=5-', 'If-Range': '"old"'})
    assert response.status_code == 200 and response.content == b'0123456789'


def test_playlist_byte_range_is_preserved_without_validator(client, monkeypatch):
    job_id, record = browser_job(client)
    record['streams'][0]['parts'][0].update(start=2, size=3)
    with DB.begin() as db:
        db.get(Job, job_id).selected = json.dumps(record)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    def serve(request):
        assert request.headers['range'] == 'bytes=2-4'
        return httpx.Response(206, headers={'Content-Range': 'bytes 2-4/10'},
                              stream=httpx.ByteStream(b'234'))
    mock_source(monkeypatch, serve)
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0')
    assert response.status_code == 206 and response.content == b'234'
    assert response.headers['content-range'] == 'bytes 0-2/3'


def test_changed_validator_instructs_client_to_restart(client, monkeypatch):
    job_id, _ = browser_job(client)
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    mock_source(monkeypatch, lambda _: httpx.Response(206,
                headers={'Content-Range': 'bytes 5-9/10', 'ETag': '"new"'}, stream=httpx.ByteStream(b'56789')))
    response = client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0',
                          headers={'Range': 'bytes=5-', 'If-Range': '"old"'})
    assert response.status_code == 409 and response.headers['x-dav-restart'] == '1'


def test_resume_with_weak_etag_uses_last_modified(client,monkeypatch):
    job_id,_=browser_job(client)
    client.post(f'/api/jobs/{job_id}/start',headers=HEADERS)
    actual=httpx.Client
    date='Mon, 26 Oct 2026 10:00:00 GMT'
    def serve(request):
        return httpx.Response(206,headers={'Content-Range':'bytes 5-9/10','ETag':'W/"x"',
                              'Last-Modified':date},stream=httpx.ByteStream(b'56789'))
    monkeypatch.setattr('app.browser_routes.httpx.Client',lambda **_:actual(transport=httpx.MockTransport(serve)))
    response=client.get(f'/api/jobs/{job_id}/client/tracks/0/parts/0',headers={'Range':'bytes=5-','If-Range':date})
    assert response.status_code==206 and response.content==b'56789'


@pytest.mark.parametrize('error,expected',[('HTTP error 429','429'),('Sign in to confirm you’re not a bot','робот'),
                                        ('Unsupported URL','не поддерживается'),('DRM protected','DRM')])
def test_reasons(error,expected):
    assert expected in explain_error(error)


def test_hls_local_plan_and_encryption(monkeypatch):
    monkeypatch.setattr('app.plans.manifest_text',lambda *_:('#EXTM3U\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST','https://example.com/v/index.m3u8'))
    assert hls_parts('https://example.com/v/index.m3u8',{})==[{'url':'https://example.com/v/a.ts'}]
    monkeypatch.setattr('app.plans.manifest_text',lambda *_:('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\na.ts\n#EXT-X-ENDLIST','https://example.com/v/index.m3u8'))
    with pytest.raises(MediaProblem,match='Зашифрованный'):
        hls_parts('https://example.com/v/index.m3u8',{})


def test_signature_ignores_expiring_urls():
    a={'source_id':'v','streams':[{'parts':[{'url':'https://a?token=old'}]}]}
    b={'source_id':'v','streams':[{'parts':[{'url':'https://a?token=new'}]}]}
    assert plan_signature(a)==plan_signature(b)

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import BrowserSession, DB, Job
from app.main import app, byte_range
from app.media import choose_formats
from app.security import public_addresses, validate_url
from app.storage import job_dir
from app.worker import change, cleanup, execute

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def new(client, **kwargs):
    response = client.post('/api/jobs', headers=HEADERS,
                           json={'url': 'https://example.com/video.mp4', 'quality': 1080} | kwargs)
    assert response.status_code == 201, response.text
    return response.json()['id']


def set_job(job_id, **values):
    with DB.begin() as db:
        job = db.get(Job, job_id)
        for key, value in values.items():
            setattr(job, key, value)


def test_one_session_many_jobs_and_isolation(client):
    ids = [new(client) for _ in range(10)]
    assert len({j['id'] for j in client.get('/api/jobs').json()}) == 10
    with DB() as db:
        assert len(db.scalars(select(BrowserSession)).all()) == 1
    assert client.post('/api/jobs', headers=HEADERS,
                       json={'url': 'https://example.com/next.mp4'}).status_code == 429
    with TestClient(app) as other:
        assert other.get('/api/jobs').json() == []
        assert other.post(f'/api/jobs/{ids[0]}/cancel', headers=HEADERS).status_code == 404
        assert other.get(f'/api/jobs/{ids[0]}/file').status_code == 404


def test_cookie_and_csrf(client):
    response = client.get('/api/jobs')
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Max-Age=34560000' in cookie
    assert client.post('/api/jobs', json={'url': 'https://example.com/x'}).status_code == 403
    assert client.get('/').status_code == 200
    assert "frame-ancestors 'none'" in client.get('/').headers['content-security-policy']


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/x',
                                 'http://user:pass@example.com/x', 'https://example.com:8080/x'])
def test_bad_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1'])
def test_egress_rejects_internal(ip, monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: [(2, 1, 6, '', (ip, 443))])
    with pytest.raises(ValueError):
        public_addresses('attacker.example', 443)


def fmt(size, **overrides):
    return {'format_id': str(size), 'url': 'https://example.com/v', 'width': size * 16 // 9,
            'height': size, 'vcodec': 'h264', 'acodec': 'aac', 'protocol': 'https', 'ext': 'mp4'} | overrides


@pytest.mark.parametrize('target,expected', [(1080, 1440), (720, 720), (2160, 1440), (0, 1440), (480, 720)])
def test_quality(target, expected):
    selected = choose_formats([fmt(360), fmt(720), fmt(1440)], target)
    assert selected[0]['height'] == expected


def test_portrait_and_drm():
    selected = choose_formats([fmt(1080, width=1080, height=1920), fmt(2160, has_drm=True)], 1080)
    assert selected[0]['width'] == 1080


def test_split_audio():
    audio = fmt(0, format_id='a', vcodec='none', acodec='aac', ext='m4a')
    assert len(choose_formats([audio, fmt(1080, acodec='none')], 1080)) == 2


def test_analysis_to_start_not_reanalysis(client, monkeypatch):
    job_id = new(client)
    monkeypatch.setattr('app.worker.inspect', lambda *_: {'title': 'Test', 'selected': '{}',
                         'actual_quality': 1080, 'extension': 'mp4', 'mode': 'direct', 'total': 10})
    execute(job_id)
    response = client.post(f'/api/jobs/{job_id}/start', headers=HEADERS)
    assert response.json()['status'] == 'ready'


def test_pause_cancel_and_ttl(client):
    job_id = new(client)
    set_job(job_id, status='downloading', phase='download', worker_id='worker')
    response = client.post(f'/api/jobs/{job_id}/pause', headers=HEADERS)
    assert response.json()['status'] == 'pausing'
    assert 604790 < response.json()['expires_at'] - time.time() <= 604800
    change(job_id, status='ready', worker_id=None)
    with DB() as db:
        assert db.get(Job, job_id).status == 'pausing'
    set_job(job_id, status='paused', worker_id=None)
    assert client.post(f'/api/jobs/{job_id}/resume', headers=HEADERS).json()['status'] == 'queued'
    set_job(job_id, worker_id=None)
    path = job_dir(job_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / 'video.part').write_bytes(b'partial')
    client.post(f'/api/jobs/{job_id}/cancel', headers=HEADERS)
    cleanup()
    assert not path.exists()
    history = client.get('/api/jobs').json()
    assert len(history) == 1 and history[0]['status'] == 'cancelled'
    assert history[0]['history_visible'] is True
    with DB() as db:
        assert len(db.scalars(select(BrowserSession)).all()) == 1


def test_session_no_ttl_and_job_expiry(client):
    job_id = new(client)
    set_job(job_id, status='paused', expires_at=time.time() - 1)
    with DB.begin() as db:
        session = db.scalar(select(BrowserSession))
        session.last_active_at = 0
    cleanup()
    with DB() as db:
        assert db.get(Job, job_id) is None
        assert db.scalar(select(BrowserSession)) is not None


def ready_file(client):
    job_id = new(client)
    path = job_dir(job_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / 'video.mp4').write_bytes(b'0123456789')
    set_job(job_id, status='ready', phase='ready', filename='video.mp4', extension='mp4', mode='cached')
    return job_id


def test_range_resume_and_completion(client):
    job_id = ready_file(client)
    head = client.head(f'/api/jobs/{job_id}/file')
    assert head.status_code == 200 and head.headers['content-length'] == '10'
    assert head.content == b''
    response = client.get(f'/api/jobs/{job_id}/file', headers={'Range': 'bytes=5-'})
    assert response.status_code == 206
    assert response.content == b'56789'
    assert response.headers['content-range'] == 'bytes 5-9/10'
    with DB() as db:
        job = db.get(Job, job_id)
        assert job.status == 'ready'
        assert 3590 < job.expires_at - time.time() <= 3600
    assert client.get(f'/api/jobs/{job_id}/file', headers={'Range': 'bytes=50-'}).status_code == 416


def test_if_range_and_duplicate_transfer(client):
    job_id = ready_file(client)
    response = client.get(f'/api/jobs/{job_id}/file', headers={'Range': 'bytes=5-', 'If-Range': 'old'})
    assert response.status_code == 200 and response.content == b'0123456789'
    set_job(job_id, status='streaming', heartbeat=time.time())
    assert client.get(f'/api/jobs/{job_id}/file').status_code == 409


def test_stale_transfer_becomes_paused(client):
    job_id = ready_file(client)
    set_job(job_id, status='streaming', heartbeat=time.time() - 100)
    cleanup()
    with DB() as db:
        assert db.get(Job, job_id).status == 'paused'


def test_crashed_cancellation_is_not_resumed(client):
    job_id = ready_file(client)
    set_job(job_id, status='cancelling', worker_id='dead-worker', heartbeat=time.time() - 100)
    cleanup()
    with DB() as db:
        assert db.get(Job, job_id) is None


@pytest.mark.parametrize('header,expected', [('bytes=-3', (7, 9, True)),
                                          ('bytes=0-2', (0, 2, True)),
                                          (None, (0, 9, False))])
def test_ranges(header, expected):
    assert byte_range(header, 10) == expected

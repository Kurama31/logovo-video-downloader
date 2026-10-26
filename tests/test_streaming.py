import json

import httpx
import pytest

from app.db import DB, Job
from app.main import app, transfer_iterator
from app.media import refresh
from fastapi.testclient import TestClient

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}


def direct_job(client):
    job_id = client.post('/api/jobs', headers=HEADERS,
                         json={'url': 'https://example.com/video.mp4'}).json()['id']
    record = {'source_id': 'v', 'extractor': 'Generic', 'ids': ['1'], 'fingerprints': [],
              'streams': [{'url': 'https://example.com/video.mp4', 'headers': {}}]}
    with DB.begin() as db:
        job = db.get(Job, job_id)
        job.status, job.mode, job.phase, job.extension = 'ready', 'direct', 'download', 'mp4'
        job.selected = json.dumps(record)
    return job_id, record


def test_direct_proxy_range_and_no_cache(monkeypatch):
    original_client = httpx.Client
    seen = []

    def serve(request):
        seen.append(request)
        if request.headers.get('range'):
            return httpx.Response(206, headers={'Content-Range': 'bytes 5-9/10',
                                  'Content-Length': '5', 'ETag': '"v1"'}, stream=httpx.ByteStream(b'56789'))
        return httpx.Response(200, headers={'Content-Length': '10', 'ETag': '"v1"'}, stream=httpx.ByteStream(b'0123456789'))

    with TestClient(app) as client:
        job_id, record = direct_job(client)
        monkeypatch.setattr('app.main.refresh', lambda _: {'selected': json.dumps(record)})
        monkeypatch.setattr('app.main.httpx.Client', lambda **_: original_client(transport=httpx.MockTransport(serve)))
        response = client.get(f'/api/jobs/{job_id}/file')
        assert response.content == b'0123456789'
        response = client.get(f'/api/jobs/{job_id}/file', headers={'Range': 'bytes=5-'})
        assert response.status_code == 206 and response.content == b'56789'
        assert seen[-1].headers['if-range'] == '"v1"'
        with DB() as db:
            assert db.get(Job, job_id).filename == ''


def test_disconnect_keeps_position_and_pause(monkeypatch):
    with TestClient(app) as client:
        job_id, _ = direct_job(client)
        with DB.begin() as db:
            db.get(Job, job_id).status = 'streaming'
        closed = []
        stream = transfer_iterator(job_id, iter([b'12345', b'67890']), 0, 9, 10, lambda: closed.append(True))
        assert next(stream) == b'12345'
        stream.close()
        with DB() as db:
            job = db.get(Job, job_id)
            assert job.status == 'paused' and job.downloaded == 5
        assert closed == [True]


def test_changed_source_rejected(monkeypatch):
    with TestClient(app) as client:
        job_id, record = direct_job(client)
        monkeypatch.setattr('app.media.inspect', lambda *_: {'selected': json.dumps(record | {'source_id': 'other'})})
        with DB() as db:
            with pytest.raises(ValueError):
                refresh(db.get(Job, job_id))

import re

from fastapi.testclient import TestClient

from app.db import DB, Job
from app.main import app
from app.worker import execute

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}


def test_unexpected_analysis_failure_is_persisted(monkeypatch):
    with TestClient(app) as client:
        response = client.post('/api/jobs', headers=HEADERS, json={'url': 'https://example.com/video'})
        job_id = response.json()['id']
        def fail(*args):
            raise RuntimeError('unexpected secret-source-token')
        monkeypatch.setattr('app.worker.inspect', fail)
        execute(job_id)
        job = client.get('/api/jobs').json()[0]
        assert job['status'] == 'error' and job['error']
        assert 'secret-source-token' not in job['error']
        with DB() as db:
            saved = db.get(Job, job_id)
            assert saved.worker_id is None
            assert saved.phase == 'analyze'


def test_unknown_api_exception_is_json_with_safe_incident_id(monkeypatch):
    def fail(url):
        raise RuntimeError('https://example.com/secret?token=private-value')
    monkeypatch.setattr('app.main.validate_url', fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post('/api/jobs', headers=HEADERS, json={'url': 'https://example.com/video'})
    assert response.status_code == 500
    data = response.json()
    assert re.fullmatch('[a-f0-9]{12}', data['incident_id'])
    assert 'Код ошибки:' in data['detail']
    assert 'private-value' not in response.text


def test_repeated_pause_is_not_an_error():
    with TestClient(app) as client:
        job_id = client.post('/api/jobs', headers=HEADERS, json={'url': 'https://example.com/video'}).json()['id']
        first = client.post(f'/api/jobs/{job_id}/pause', headers=HEADERS)
        second = client.post(f'/api/jobs/{job_id}/pause', headers=HEADERS)
        assert first.status_code == second.status_code == 200
        assert second.json()['status'] == 'paused'
        assert second.json()['expires_at'] == first.json()['expires_at']

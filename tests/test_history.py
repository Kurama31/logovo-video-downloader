import json
import time

import pytest
from fastapi.testclient import TestClient

from app.db import DB, Job
from app.main import app
from app.storage import job_dir
from app.worker import change, cleanup

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def create(client):
    response = client.post('/api/jobs', headers=HEADERS,
                           json={'url': 'https://example.com/video.mp4'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_cancel_keeps_error_and_date_even_after_cleanup(client):
    job_id = create(client)
    change(job_id, status='error', error='Источник вернул HTTP 500.')
    before = time.time()
    cancelled = client.post(f'/api/jobs/{job_id}/cancel', headers=HEADERS).json()
    assert cancelled['status'] == 'cancelled'
    assert cancelled['finished_at'] >= before
    cleanup()
    history = client.get('/api/jobs').json()
    assert len(history) == 1
    assert history[0]['error'] == 'Источник вернул HTTP 500.'
    assert history[0]['finished_at'] == cancelled['finished_at']
    assert history[0]['data_expired'] is True
    assert history[0]['expires_at'] is None


def test_clear_url_does_not_erase_visible_error(client):
    job_id = create(client)
    change(job_id, status='error', error='Недоступный источник.')
    response = client.post(f'/api/jobs/{job_id}/discard', headers=HEADERS)
    assert response.json()['status'] == 'error'
    assert client.get('/api/jobs').json()[0]['history_visible'] is True


def test_discard_analysis_does_not_create_cancelled_history(client):
    job_id = create(client)
    assert client.post(f'/api/jobs/{job_id}/discard', headers=HEADERS).status_code == 200
    assert client.get('/api/jobs').json() == []
    cleanup()
    with DB() as db:
        assert db.get(Job, job_id) is None


@pytest.mark.parametrize('status', ['paused', 'error', 'completed'])
def test_expired_data_is_removed_but_history_survives(client, status):
    job_id = create(client)
    with DB.begin() as db:
        job = db.get(Job, job_id)
        job.status = status
        job.history_visible = True
        job.error = 'Исходная причина.' if status == 'error' else ''
        job.selected = json.dumps({'streams': [{'url': 'https://example.com/signed-secret'}]})
        job.expires_at = time.time() - 1
    path = job_dir(job_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / 'temporary.part').write_bytes(b'part')
    cleanup()
    assert not path.exists()
    history = client.get('/api/jobs').json()
    assert len(history) == 1 and history[0]['data_expired'] is True
    if status == 'error':
        assert history[0]['error'] == 'Исходная причина.'
    assert client.post(f'/api/jobs/{job_id}/resume', headers=HEADERS).status_code == 410
    with DB() as db:
        assert 'signed-secret' not in db.get(Job, job_id).selected


def test_forget_is_explicit_and_cannot_remove_active_job(client):
    job_id = create(client)
    assert client.post(f'/api/jobs/{job_id}/forget', headers=HEADERS).status_code == 409
    client.post(f'/api/jobs/{job_id}/cancel', headers=HEADERS)
    cleanup()
    assert len(client.get('/api/jobs').json()) == 1
    assert client.post(f'/api/jobs/{job_id}/forget', headers=HEADERS).status_code == 200
    cleanup()
    assert client.get('/api/jobs').json() == []
    with DB() as db:
        assert db.get(Job, job_id) is None


def test_old_errors_do_not_exhaust_active_quota(client):
    for _ in range(12):
        change(create(client), status='error', error='Источник недоступен.')
    assert len(client.get('/api/jobs').json()) == 12
    assert create(client)

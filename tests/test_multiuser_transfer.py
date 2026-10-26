import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient

from app.db import DB, Job
from app.main import app

HEADERS = {'Origin': 'http://testserver', 'X-Requested-With': 'DAV'}


def prepare(client, name, size):
    response = client.post('/api/jobs', headers=HEADERS,
                           json={'url': f'https://example.com/{name}.mp4'})
    response.raise_for_status()
    job_id = response.json()['id']
    with DB.begin() as db:
        job = db.get(Job, job_id)
        job.mode = 'browser'
        job.status = 'awaiting'
        job.phase = 'download'
        job.extension = 'mp4'
        job.selected = json.dumps({'streams': [{'headers': {}, 'parts': [
            {'url': f'https://example.com/{name}.mp4', 'size': size}]}]})
    client.post(f'/api/jobs/{job_id}/start', headers=HEADERS).raise_for_status()
    return job_id


def test_two_sessions_stream_different_files_concurrently_without_cross_access(monkeypatch):
    original_client = httpx.Client
    entered = threading.Barrier(2)
    midway = threading.Barrier(2)
    payloads = {'/alice.mp4': b'A' * 16384, '/bob.mp4': b'B' * 16384}

    class ConcurrentBody(httpx.SyncByteStream):
        def __init__(self, payload):
            self.payload = payload

        def __iter__(self):
            entered.wait(timeout=10)
            yield self.payload[:8192]
            midway.wait(timeout=10)
            yield self.payload[8192:]

    def serve(request):
        payload = payloads[request.url.path]
        return httpx.Response(200, headers={'Content-Length': str(len(payload))},
                              stream=ConcurrentBody(payload))

    with TestClient(app) as alice, TestClient(app) as bob:
        alice_id = prepare(alice, 'alice', 16384)
        bob_id = prepare(bob, 'bob', 16384)
        assert {job['id'] for job in alice.get('/api/jobs').json()} == {alice_id}
        assert {job['id'] for job in bob.get('/api/jobs').json()} == {bob_id}
        endpoint = lambda job_id: f'/api/jobs/{job_id}/client/tracks/0/parts/0'
        assert alice.get(endpoint(bob_id)).status_code == 404
        assert bob.get(endpoint(alice_id)).status_code == 404
        monkeypatch.setattr('app.browser_routes.httpx.Client',
                            lambda **_: original_client(transport=httpx.MockTransport(serve)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(alice.get, endpoint(alice_id))
            second = pool.submit(bob.get, endpoint(bob_id))
            alice_result, bob_result = first.result(timeout=15), second.result(timeout=15)
        assert alice_result.status_code == bob_result.status_code == 200
        assert alice_result.content == payloads['/alice.mp4']
        assert bob_result.content == payloads['/bob.mp4']

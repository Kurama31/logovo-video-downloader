"""Uses generated test media only; no third-party video or internet dependency."""
import functools
import json
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.db import BrowserSession, DB, Job
from app.media import inspect
from app.security import token_hash
from app.storage import job_dir
from app.worker import execute


def test_real_hls_download_and_mux(tmp_path, monkeypatch):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('Integration requires ffmpeg; included in Docker image')
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=320x240:r=10',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'hls',
                    '-hls_time', '1', '-hls_list_size', '0', str(tmp_path / 'index.m3u8')], check=True)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr('app.media.PROXY_URL', '')
    monkeypatch.setattr('app.plans.PROXY_URL', '')
    try:
        url = f'http://127.0.0.1:{server.server_port}/index.m3u8'
        # Nonstandard ports are permitted only for this generated local fixture.
        monkeypatch.setattr('app.media.validate_url', lambda value: value)
        monkeypatch.setattr('app.plans.validate_url', lambda value: value)
        result = inspect(url, 1080)
        assert result['mode'] == 'browser'
        assert json.loads(result['selected'])['remux'] is True
        assert len(json.loads(result['selected'])['streams'][0]['parts']) >= 1
        with DB.begin() as db:
            owner = BrowserSession(token_hash=token_hash('integration'))
            db.add(owner)
            db.flush()
            job = Job(owner_id=owner.id, url=url, quality=1080, phase='analyze', status='analyzing')
            db.add(job)
            db.flush()
            job_id = job.id
        execute(job_id)
        with DB() as db:
            job = db.get(Job, job_id)
            assert job.status == 'awaiting', job.error
            assert job.mode == 'browser'
            assert job.filename == ''
            assert not job_dir(job_id).exists()
    finally:
        server.shutdown()
        server.server_close()

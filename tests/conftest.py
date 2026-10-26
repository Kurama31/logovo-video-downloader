import os
import tempfile
from pathlib import Path

import pytest

test_root = Path(tempfile.mkdtemp(prefix='dav-tests-'))
os.environ['DATABASE_URL'] = 'sqlite:///' + (test_root / 'test.db').as_posix()
os.environ['DATA_DIR'] = str(test_root / 'data')
os.environ['PUBLIC_ORIGIN'] = 'http://testserver'
os.environ['COOKIE_SECURE'] = 'false'

from app.db import Base, engine


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def no_player_fingerprinting_network(monkeypatch):
    # Worker tests must not contact third-party pages for optional UI diagnostics.
    from app.player_detection import blank_detection
    monkeypatch.setattr('app.worker.detect_player', lambda url: blank_detection())

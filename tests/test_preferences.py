from fastapi.testclient import TestClient

from app.main import app
from app.db import DB, SessionPreference, Job
from sqlalchemy import select

HEADERS={'Origin':'http://testserver','X-Requested-With':'DAV'}


def test_default_and_persisted_destination():
    with TestClient(app) as client:
        assert client.get('/api/preferences').json()=={'mode':'browser','label':'Папка загрузок браузера'}
        response=client.post('/api/preferences',headers=HEADERS,json={'mode':'browser','label':'ignored'})
        assert response.status_code==200
        assert response.json()['label']=='Папка загрузок браузера'
        custom={'mode':'directory','label':'Мои видео'}
        assert client.post('/api/preferences',headers=HEADERS,json=custom).json()==custom
        assert client.get('/api/preferences').json()==custom
        with DB() as db:
            pref=db.scalar(select(SessionPreference))
            assert pref.destination_label=='Мои видео'
        with TestClient(app) as other:
            assert other.get('/api/preferences').json()['mode']=='browser'
            other.post('/api/preferences',headers=HEADERS,json={'mode':'directory','label':'Другая папка'})
        assert client.get('/api/preferences').json()==custom


def test_preference_validation():
    with TestClient(app) as client:
        assert client.post('/api/preferences',json={'mode':'directory','label':'x'}).status_code==403
        for value in [{'mode':'invalid','label':'x'},{'mode':'directory','label':'x\nheader'},
                      {'mode':'directory','label':' '},{'mode':'directory','label':'x'*256}]:
            assert client.post('/api/preferences',headers=HEADERS,json=value).status_code==422


def test_withdraw_preferences_is_scoped_and_preserves_downloads():
    with TestClient(app) as client:
        client.post('/api/preferences',headers=HEADERS,json={'mode':'directory','label':'Мои видео'})
        job=client.post('/api/jobs',headers=HEADERS,json={'url':'https://example.com/video.mp4'}).json()
        with TestClient(app) as other:
            other.post('/api/preferences',headers=HEADERS,json={'mode':'directory','label':'Другая папка'})
            assert client.delete('/api/preferences').status_code==403
            assert client.delete('/api/preferences',headers=HEADERS).status_code==200
            assert client.get('/api/preferences').json()['mode']=='browser'
            assert other.get('/api/preferences').json()['label']=='Другая папка'
        with DB() as db:
            assert db.get(Job,job['id']) is not None
            assert len(db.scalars(select(SessionPreference)).all())==1
        assert client.delete('/api/preferences',headers=HEADERS).status_code==200

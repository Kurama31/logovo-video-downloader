import json

from app.db import Job
from app.main import public
from app.media import inspect


def test_public_reports_stage_progress_without_source_urls():
    job=Job(id='test',owner_id='owner',url='https://example.com',status='analyzing',phase='analyze',
            selected=json.dumps({'analysis_progress':50}))
    assert public(job)['analysis_percent']==50
    assert 'selected' not in public(job)
    job.status='error'
    assert public(job)['analysis_percent']==100
    job.status='awaiting';job.phase='download'
    assert public(job)['analysis_percent']==100


def test_analysis_progress_follows_completed_stages(monkeypatch):
    events=[]
    class Extractor:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):
            assert events==[25]
            return {'id':'v','title':'Test','formats':[{'format_id':'1','url':'https://example.com/v.mp4',
                    'protocol':'https','ext':'mp4','vcodec':'h264','acodec':'aac','height':720,'width':1280}]}
    monkeypatch.setattr('app.media.yt_dlp.YoutubeDL',Extractor)
    assert inspect('https://example.com/v',1080,events.append)['mode']=='browser'
    assert events==[25,50,75]

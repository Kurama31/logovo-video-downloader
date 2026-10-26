import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import PAUSE_SECONDS
from app.db import DB, BrowserSession, Job
from app.main import owned, public, session

router = APIRouter()


class CatalogSelection(BaseModel):
    voice_id: str = Field(min_length=1, max_length=100)
    episodes: list[str] | None = Field(default=None, max_length=100)
    container: str = 'source'


@router.post('/api/catalogs/{job_id}/selection', status_code=201)
def select_episodes(job_id: str, body: CatalogSelection, owner_id=Depends(session)):
    if body.container not in ('source', 'mp4', 'mkv'):
        raise HTTPException(422, 'Выберите исходный контейнер, MP4 или MKV.')
    with DB.begin() as db:
        db.scalar(select(BrowserSession).where(BrowserSession.id == owner_id).with_for_update())
        parent = owned(db, job_id, owner_id, lock=True)
        if parent.status != 'selection':
            raise HTTPException(409, 'Каталог уже использован или недоступен. Вставьте ссылку заново.')
        catalog = json.loads(parent.selected)['catalog']
        group = next((item for item in catalog['groups'] if item['id'] == body.voice_id), None)
        if group is None:
            raise HTTPException(422, 'Выбранная озвучка отсутствует в каталоге.')
        numbers = group['available'] if body.episodes is None else list(dict.fromkeys(n.strip() for n in body.episodes))
        if not numbers or len(numbers) > 100:
            raise HTTPException(422, 'Выберите от 1 до 100 серий.')
        missing = [n for n in numbers if n not in group['sources']]
        if missing:
            raise HTTPException(422, 'Эти серии недоступны через поддерживаемый плеер: ' + ', '.join(missing)[:300])
        count = db.scalar(select(func.count()).select_from(Job).where(Job.owner_id == owner_id,
                          Job.status.not_in(['completed', 'cancelled', 'selection', 'error']), Job.data_expired.is_(False)))
        if count + len(numbers) > 100:
            raise HTTPException(429, 'Не более 100 незавершённых заданий в сессии. Удалите ненужные.')
        jobs = []
        for number in numbers:
            source = group['sources'][number]
            title = source.get('title') or f'{catalog["title"]} — серия {number} — {group["label"]}'
            selection = {**source, 'container': body.container, 'title': title}
            # Embedded sources must be rediscovered from the original page when signed URLs expire.
            job_url = parent.url if source.get('kind', '').startswith('embedded_') else source.get('url') or parent.url
            job = Job(owner_id=owner_id, url=job_url, quality=parent.quality,
                      status='pending', phase='analyze', mode='browser', title=title, history_visible=True,
                      selected=json.dumps({'selection': selection}), expires_at=time.time() + PAUSE_SECONDS)
            db.add(job)
            jobs.append(job)
        parent.status = 'cancelled'
        db.flush()
        return {'jobs': [public(job) for job in jobs]}

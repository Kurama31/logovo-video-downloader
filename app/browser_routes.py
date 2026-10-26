import json
import re
import time

import httpx
from sqlalchemy import select
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import MAX_FILE_BYTES, PAUSE_SECONDS, PROXY_URL, READY_SECONDS
from app.db import BrowserSession, DB, Job, SessionPreference
from app.main import owned, public, session
from app.media import refresh
from app.plans import explain_error, plan_signature

router = APIRouter()


class DestinationPreference(BaseModel):
    mode: str = 'browser'
    label: str = Field(default='Папка загрузок браузера', min_length=1, max_length=255)


@router.get('/api/preferences')
def preferences(owner_id=Depends(session)):
    with DB() as db:
        pref = db.get(SessionPreference, owner_id)
        return {'mode': pref.destination_mode if pref else 'browser',
                'label': pref.destination_label if pref else 'Папка загрузок браузера'}


@router.post('/api/preferences')
def update_preferences(body: DestinationPreference, owner_id=Depends(session)):
    if body.mode not in ('browser', 'directory') or any(ord(c) < 32 or ord(c) == 127 for c in body.label):
        raise HTTPException(422, 'Некорректная настройка папки.')
    label = 'Папка загрузок браузера' if body.mode == 'browser' else body.label.strip()
    if not label:
        raise HTTPException(422, 'Название папки не может быть пустым.')
    with DB.begin() as db:
        # Lock the owner so concurrent tabs cannot create duplicate preference rows.
        db.scalar(select(BrowserSession).where(BrowserSession.id == owner_id).with_for_update())
        pref = db.get(SessionPreference, owner_id)
        if pref is None:
            pref = SessionPreference(owner_id=owner_id)
            db.add(pref)
        pref.destination_mode = body.mode
        pref.destination_label = label
        pref.updated_at = time.time()
    return {'mode': body.mode, 'label': label}


@router.delete('/api/preferences')
def delete_preferences(owner_id=Depends(session)):
    with DB.begin() as db:
        pref = db.get(SessionPreference, owner_id)
        if pref is not None:
            db.delete(pref)
    return {'mode': 'browser', 'label': 'Папка загрузок браузера'}


@router.get('/api/jobs/{job_id}/client/plan')
def browser_plan(job_id: str, owner_id=Depends(session)):
    with DB() as db:
        job = owned(db, job_id, owner_id)
        if job.mode != 'browser' or job.status not in ('client_downloading', 'paused', 'awaiting'):
            raise HTTPException(409, 'Задание не готово к локальному скачиванию.')
    try:
        fresh = refresh(job)
    except Exception as exc:
        raise HTTPException(422, explain_error(exc)) from exc
    record = json.loads(fresh['selected'])
    with DB.begin() as db:
        saved = owned(db, job_id, owner_id, lock=True)
        if saved.status not in ('client_downloading', 'paused', 'awaiting'):
            raise HTTPException(409, 'Состояние задания изменилось.')
        saved.selected = fresh['selected']
    return {'signature': plan_signature(record), 'remux': record.get('remux', True),
            'extension': job.extension, 'max_bytes': MAX_FILE_BYTES,
            'tracks': [{'ext': stream.get('ext'), 'parts': [{'size': p.get('size')} for p in stream['parts']]}
                       for stream in record['streams']]}


class ClientProgress(BaseModel):
    downloaded: int = Field(ge=0, le=MAX_FILE_BYTES)
    total: int | None = Field(default=None, ge=0, le=MAX_FILE_BYTES)
    phase: str = 'download'
    speed: float | None = Field(default=None, ge=0, le=10**12)


@router.post('/api/jobs/{job_id}/client/progress')
def browser_progress(job_id: str, body: ClientProgress, owner_id=Depends(session)):
    if body.phase not in ('download', 'mux', 'save', 'complete'):
        raise HTTPException(422, 'Неизвестный этап загрузки.')
    with DB.begin() as db:
        job = owned(db, job_id, owner_id, lock=True)
        if job.mode != 'browser' or job.status != 'client_downloading':
            raise HTTPException(409, 'Загрузка приостановлена или отменена.')
        job.downloaded = body.downloaded
        job.total = body.total
        job.phase = body.phase
        job.speed = body.speed
        job.heartbeat = job.updated_at = time.time()
        job.expires_at = time.time() + PAUSE_SECONDS
        if body.phase == 'complete':
            job.status = 'completed'
            job.finished_at = time.time()
            job.history_visible = True
            job.speed = None
            job.expires_at = time.time() + READY_SECONDS
        return public(job)


@router.get('/api/jobs/{job_id}/client/tracks/{track_id}/parts/{part_id}')
def browser_part(job_id: str, track_id: int, part_id: int, request: Request,
                 response: Response, owner_id=Depends(session)):
    with DB() as db:
        job = owned(db, job_id, owner_id)
        if job.status != 'client_downloading' or job.mode != 'browser':
            raise HTTPException(409, 'Загрузка не запущена или приостановлена.')
        streams = json.loads(job.selected).get('streams', [])
        if track_id < 0 or track_id >= len(streams) or part_id < 0 or part_id >= len(streams[track_id]['parts']):
            raise HTTPException(404, 'Фрагмент не найден.')
        stream = streams[track_id]
        part = stream['parts'][part_id]
    headers = {k: v for k, v in stream.get('headers', {}).items()
               if k.lower() not in ('range', 'if-range', 'host', 'connection', 'accept-encoding')}
    headers['Accept-Encoding'] = 'identity'
    base = part.get('start', 0)
    size = part.get('size')
    bounded = 'start' in part  # A playlist byte range, not an ordinary file's known size.
    requested = request.headers.get('range')
    offset, end = 0, None
    if requested:
        match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested)
        if not match:
            raise HTTPException(416, 'Некорректный диапазон.')
        offset = int(match[1])
        end = int(match[2]) if match[2] else None
        if (end is not None and end < offset) or (size is not None and offset >= size):
            raise HTTPException(416, 'Диапазон вне фрагмента.')
    if size is not None:
        end = min(end, size - 1) if end is not None else size - 1
    if requested or bounded:
        headers['Range'] = f'bytes={base + offset}-' + (str(base + end) if end is not None else '')
    validator = request.headers.get('if-range')
    # Same-run source-driven ranges can lack validators. The client checks overlap;
    # after pause/reload it restarts an unvalidated incomplete part from zero.
    if validator:
        headers['If-Range'] = validator
    client = httpx.Client(proxy=PROXY_URL or None, trust_env=False, follow_redirects=True,
                          max_redirects=5, timeout=30)
    remote = None
    try:
        remote = client.send(client.build_request('GET', part['url'], headers=headers), stream=True)
        if 'Range' not in headers and remote.status_code in (400, 416):
            # Some media servers require Range even for the initial request.
            remote.close()
            headers['Range'] = 'bytes=0-' + (str(end) if end is not None else '')
            remote = client.send(client.build_request('GET', part['url'], headers=headers), stream=True)
        if remote.status_code not in (200, 206):
            raise HTTPException(502, explain_error(f'HTTP error {remote.status_code}'))
        if remote.headers.get('content-encoding', 'identity') != 'identity':
            raise HTTPException(502, 'Источник не поддерживает передачу исходных байтов.')
        if bounded and remote.status_code != 206:
            raise HTTPException(409, 'Источник не поддерживает диапазон сегмента, указанный в плейлисте.')
        length = int(remote.headers['content-length']) if remote.headers.get('content-length') else None
        response_headers = {}
        if remote.status_code == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', remote.headers.get('content-range', ''))
            if not match or int(match[1]) != base + offset:
                raise HTTPException(502, 'Источник вернул неверный диапазон байтов.')
            first, last, full = map(int, match.groups())
            if first > last or last >= full or (end is not None and last > base + end):
                raise HTTPException(502, 'Источник вернул неверные границы видеоданных.')
            if size is not None and ((bounded and base + size > full) or (not bounded and full != size)):
                raise HTTPException(409, 'Размер видео изменился. Вставьте ссылку заново.')
            part_total = size if size is not None else full
            response_headers['Content-Range'] = f'bytes {offset}-{last-base}/{part_total}'
            length = last - first + 1
        elif size is not None and length is not None and length != size:
            raise HTTPException(502, 'Источник не вернул нужный фрагмент целиком.')
        if validator and remote.status_code == 206:
            current = remote.headers.get('etag' if validator.startswith('"') else 'last-modified')
            if current and current != validator:
                raise HTTPException(409, 'Источник изменил файл; фрагмент будет получен заново.',
                                    headers={'X-DAV-Restart': '1'})
        if length is not None:
            if length > MAX_FILE_BYTES:
                raise HTTPException(413, 'Превышен лимит размера.')
            response_headers['Content-Length'] = str(length)
        for name in ('etag', 'last-modified'):
            if remote.headers.get(name):
                response_headers[name] = remote.headers[name]
        for value in response.headers.getlist('set-cookie'):
            response_headers['Set-Cookie'] = value

        def body():
            count = 0
            check = time.monotonic()
            try:
                for block in remote.iter_raw(128 * 1024):
                    if time.monotonic() - check > 0.5:
                        with DB() as db:
                            current = db.get(Job, job_id)
                            if not current or current.status != 'client_downloading':
                                return
                        check = time.monotonic()
                    count += len(block)
                    if count > MAX_FILE_BYTES:
                        raise RuntimeError('Stream exceeds configured byte limit')
                    yield block
            finally:
                remote.close()
                client.close()

        return StreamingResponse(body(), status_code=remote.status_code,
                                 headers=response_headers, media_type='application/octet-stream')
    except Exception:
        if remote is not None:
            remote.close()
        client.close()
        raise

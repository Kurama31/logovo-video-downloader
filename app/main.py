import json
import logging
import mimetypes
import re
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.config import (COOKIE_SECURE, DATA_DIR, MAX_FILE_BYTES, MAX_SESSION_JOBS,
                        PAUSE_SECONDS, PROXY_URL, PUBLIC_ORIGIN, READY_SECONDS,
                        SESSION_COOKIE, WEB_DIR)
from app.db import BrowserSession, DB, Job, init_db
from app.media import refresh
from app.security import token_hash, validate_url
from app.storage import job_dir


@asynccontextmanager
async def lifespan(app):
    init_db()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title='Download All Videos', lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=WEB_DIR), name='static')


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    incident = secrets.token_hex(6)
    # Log a correlation ID/type, not source URLs, cookie values or exception arguments.
    logging.getLogger(__name__).error('Unexpected API error incident=%s method=%s type=%s',
                                     incident, request.method, type(exc).__name__)
    return JSONResponse({'detail': f'Внутренняя ошибка сервера. Повторите попытку. Код ошибки: {incident}.',
                         'incident_id': incident}, status_code=500, headers={'Cache-Control': 'no-store'})


@app.middleware('http')
async def headers_and_origin(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('origin') != PUBLIC_ORIGIN or request.headers.get('x-requested-with') != 'DAV':
            return JSONResponse({'detail': 'Недопустимый источник запроса.'}, status_code=403)
    response = await call_next(request)
    response.headers.update({'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                             'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def session(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE, '')
    if len(token) > 128:
        token = ''
    with DB.begin() as db:
        owner = db.scalar(select(BrowserSession).where(BrowserSession.token_hash == token_hash(token))) if token else None
        if owner is None:
            token = secrets.token_urlsafe(32)
            owner = BrowserSession(token_hash=token_hash(token))
            db.add(owner)
            db.flush()
        if time.time() - owner.last_active_at > 60:
            owner.last_active_at = time.time()
        owner_id = owner.id
    response.set_cookie(SESSION_COOKIE, token, max_age=400 * 86400, httponly=True,
                        secure=COOKIE_SECURE, samesite='lax', path='/')
    return owner_id


def owned(db, job_id, owner_id, lock=False):
    query = select(Job).where(Job.id == job_id, Job.owner_id == owner_id)
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if not job:
        raise HTTPException(404, 'Задание не найдено или срок хранения истёк.')
    return job


def public(job):
    from app.player_detection import public_detection
    values = {key: getattr(job, key) for key in ('id', 'title', 'quality', 'actual_quality', 'status',
            'phase', 'mode', 'extension', 'downloaded', 'total', 'speed', 'eta', 'error',
            'created_at', 'updated_at', 'expires_at', 'finished_at', 'history_visible', 'data_expired')}
    values['analysis_percent'] = (100 if job.status == 'error' or job.phase != 'analyze' else
                                  json.loads(job.selected or '{}').get('analysis_progress', 0))
    values['batch'] = bool(json.loads(job.selected or '{}').get('selection'))
    values['player_detection'] = public_detection(json.loads(job.selected or '{}').get('player_detection'))
    if job.status == 'selection':
        from app.catalog import public_catalog
        values['catalog'] = public_catalog(json.loads(job.selected)['catalog'])
    return values


@app.get('/')
def index():
    return FileResponse(WEB_DIR / 'index.html')


@app.get('/health')
def health():
    with DB() as db:
        db.execute(select(1))
    return {'status': 'ok'}


class NewJob(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    quality: int = 1080


@app.get('/api/jobs')
def jobs(owner_id=Depends(session)):
    with DB() as db:
        return [public(job) for job in db.scalars(select(Job).where(Job.owner_id == owner_id,
                or_(Job.status != 'cancelled', Job.history_visible.is_(True))).order_by(Job.created_at.desc()))]


@app.post('/api/jobs', status_code=201)
def create(body: NewJob, owner_id=Depends(session)):
    if body.quality not in (0, 360, 480, 720, 1080, 1440, 2160, 4320):
        raise HTTPException(422, 'Недопустимое качество.')
    try:
        url = validate_url(body.url.strip())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with DB.begin() as db:
        # Serialize creations within a session so concurrent tabs cannot evade the quota.
        db.scalar(select(BrowserSession).where(BrowserSession.id == owner_id).with_for_update())
        count = db.scalar(select(func.count()).select_from(Job).where(Job.owner_id == owner_id,
                          Job.status.in_(['analyzing','queued','downloading','client_downloading','streaming','pausing'])))
        if count >= MAX_SESSION_JOBS:
            raise HTTPException(429, f'Не более {MAX_SESSION_JOBS} заданий. Отмените ненужные.')
        job = Job(owner_id=owner_id, url=url, quality=body.quality, expires_at=time.time() + PAUSE_SECONDS)
        db.add(job)
        db.flush()
        return public(job)


@app.post('/api/jobs/{job_id}/{action}')
def action(job_id: str, action: str, owner_id=Depends(session)):
    with DB.begin() as db:
        job = owned(db, job_id, owner_id, lock=True)
        if action in ('discard', 'forget'):
            if action == 'discard' and job.history_visible:
                return public(job)  # Clearing an input must not erase a visible download/error.
            if action == 'forget' and (job.worker_id or job.status not in ('completed','error','cancelled')):
                raise HTTPException(409, 'Сначала завершите или отмените задание.')
            job.history_visible = False
            job.data_expired = False  # Allow the cleanup worker to remove explicitly hidden records.
            job.status = 'cancelling' if job.worker_id else 'cancelled'
        elif action == 'cancel':
            job.history_visible = True
            job.finished_at = time.time()
            job.status = 'cancelling' if job.worker_id else 'cancelled'
        elif action == 'pause':
            if job.status in ('paused', 'pausing'):
                return public(job)  # Client teardown and the pause button may confirm the same pause.
            if job.status not in ('analyzing', 'queued', 'downloading', 'ready', 'streaming', 'client_downloading'):
                raise HTTPException(409, 'Задание сейчас нельзя приостановить.')
            job.status = 'pausing' if job.worker_id else 'paused'
        elif action in ('start', 'resume'):
            if job.data_expired or (job.expires_at is not None and job.expires_at < time.time()):
                raise HTTPException(410, 'Срок хранения временных данных истёк. История сохранена; для скачивания вставьте ссылку заново.')
            if job.status not in ('awaiting', 'paused', 'error', 'pending'):
                raise HTTPException(409, 'Дождитесь изменения состояния задания.')
            if job.phase == 'analyze':
                count = db.scalar(select(func.count()).select_from(Job).where(Job.owner_id == owner_id,
                                  Job.status.in_(['analyzing', 'queued', 'downloading', 'client_downloading'])))
                if count >= MAX_SESSION_JOBS:
                    raise HTTPException(429, 'Слишком много активных загрузок. Приостановите ненужные.')
                job.status = 'analyzing'
            elif job.mode == 'browser':
                job.status = 'client_downloading'
                job.heartbeat = time.time()
            elif job.filename or job.mode == 'direct':
                job.status = 'ready'
            else:
                job.status = 'queued'
                job.phase = 'download'
            job.error = ''
            job.history_visible = True
            job.finished_at = None
        else:
            raise HTTPException(404, 'Действие не найдено.')
        job.speed = job.eta = None
        job.updated_at = time.time()
        job.expires_at = time.time() + PAUSE_SECONDS
        return public(job)


def byte_range(header, size):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()):
        raise HTTPException(416, 'Неподдерживаемый диапазон.', headers={'Content-Range': f'bytes */{size}'})
    left, right = match.groups()
    if not left:
        start, end = max(0, size - int(right)), size - 1
    else:
        start, end = int(left), min(int(right) if right else size - 1, size - 1)
    if start > end or start >= size or size <= 0:
        raise HTTPException(416, 'Диапазон вне файла.', headers={'Content-Range': f'bytes */{size}'})
    return start, end, True


def disposition(job):
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', job.title).strip('. ')[:150] or 'video'
    ext = re.sub(r'[^a-zA-Z0-9]', '', job.extension) or 'bin'
    return f"attachment; filename=video.{ext}; filename*=UTF-8''{quote(title + '.' + ext)}"


def transfer_state(job_id, downloaded, speed, final=False, complete=False):
    with DB.begin() as db:
        job = db.get(Job, job_id)
        if not job or job.status != 'streaming':
            return False
        job.downloaded = downloaded
        job.speed = speed
        job.heartbeat = time.time()
        if final:
            job.status = 'ready' if complete else 'paused'
            job.speed = None
            job.expires_at = time.time() + (READY_SECONDS if complete else PAUSE_SECONDS)
        return True


def acquire_transfer(job_id, owner_id):
    with DB.begin() as db:
        job = owned(db, job_id, owner_id, lock=True)
        if job.status != 'ready':
            raise HTTPException(409, 'Сначала продолжите задание или дождитесь готовности файла.')
        job.status = 'streaming'
        job.heartbeat = time.time()
        job.expires_at = None
        return job


def transfer_iterator(job_id, blocks, start, end, total, close):
    received = start
    last = time.monotonic()
    last_received = received
    complete = False
    try:
        for block in blocks:
            now = time.monotonic()
            if now - last >= 0.5:
                if not transfer_state(job_id, received, (received - last_received) / max(now - last, .001)):
                    return
                last, last_received = now, received
            if received + len(block) > MAX_FILE_BYTES:
                return
            received += len(block)
            yield block
        complete = total is None or (received == total and end == total - 1)
    finally:
        close()
        transfer_state(job_id, received, None, final=True, complete=complete)


def cached_response(job, request):
    path = job_dir(job.id) / job.filename
    if path.parent != job_dir(job.id) or not path.is_file():
        raise HTTPException(410, 'Временный файл больше недоступен.')
    size = path.stat().st_size
    etag = f'"{job.id}-{size}-{path.stat().st_mtime_ns}"'
    range_header = request.headers.get('range')
    if request.headers.get('if-range') not in (None, etag):
        range_header = None
    start, end, partial = byte_range(range_header, size)
    file = path.open('rb')
    file.seek(start)

    def blocks():
        remaining = end - start + 1
        while remaining:
            block = file.read(min(256 * 1024, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block

    headers = {'Content-Length': str(end - start + 1), 'Accept-Ranges': 'bytes',
               'ETag': etag, 'Content-Disposition': disposition(job)}
    if partial:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    return StreamingResponse(transfer_iterator(job.id, blocks(), start, end, size, file.close),
                             status_code=206 if partial else 200, headers=headers,
                             media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


def direct_response(job, request):
    fresh = refresh(job)
    old_record = json.loads(job.selected)
    record = json.loads(fresh['selected'])
    stream = record['streams'][0]
    client = httpx.Client(proxy=PROXY_URL or None, trust_env=False, follow_redirects=True,
                          max_redirects=5, timeout=httpx.Timeout(30, read=30))
    upstream = None
    try:
        headers = {k: v for k, v in stream['headers'].items()
                   if k.lower() not in ('host', 'range', 'if-range', 'accept-encoding', 'connection')}
        headers['Accept-Encoding'] = 'identity'
        range_header = request.headers.get('range')
        if range_header:
            if not re.fullmatch(r'bytes=\d+-\d*', range_header):
                raise HTTPException(416, 'Для прямой передачи поддерживается один диапазон байтов.')
            validator = old_record.get('etag') or old_record.get('last_modified')
            if not validator:
                raise HTTPException(409, 'Источник не позволяет проверить продолжение. Запустите скачивание заново.')
            headers.update({'Range': range_header, 'If-Range': validator})
        upstream = client.send(client.build_request('GET', stream['url'], headers=headers), stream=True)
        if upstream.status_code not in (200, 206):
            raise HTTPException(502, 'Источник временно не отдаёт файл.')
        if upstream.headers.get('content-encoding', 'identity') != 'identity':
            raise HTTPException(502, 'Источник использует неподдерживаемое кодирование передачи.')
        if range_header and upstream.status_code != 206:
            raise HTTPException(409, 'Источник изменил файл или не поддерживает продолжение. Скачайте заново.')
        if range_header:
            key = 'etag' if old_record.get('etag') else 'last-modified'
            if upstream.headers.get(key) and upstream.headers[key] != validator:
                raise HTTPException(409, 'Источник изменил файл. Запустите скачивание заново.')
        total = None
        start = 0
        end = -1
        if upstream.status_code == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', upstream.headers.get('content-range', ''))
            if not match:
                raise HTTPException(502, 'Источник вернул некорректный диапазон.')
            start, end, total = map(int, match.groups())
            if start != int(range_header.split('=')[1].split('-')[0]):
                raise HTTPException(502, 'Источник вернул другую часть файла.')
        elif upstream.headers.get('content-length'):
            total = int(upstream.headers['content-length'])
            end = total - 1
        if total and total > MAX_FILE_BYTES:
            raise HTTPException(413, 'Файл превышает допустимый размер.')
        for key, remote in [('etag', 'etag'), ('last_modified', 'last-modified')]:
            if upstream.headers.get(remote):
                record[key] = upstream.headers[remote]
        if record.get('etag', '').startswith('W/'):
            record.pop('etag')
        with DB.begin() as db:
            saved = db.get(Job, job.id)
            saved.selected = json.dumps(record)
            saved.total = total
        result_headers = {'Content-Disposition': disposition(job)}
        for key in ('content-length', 'content-range', 'accept-ranges', 'etag', 'last-modified'):
            if upstream.headers.get(key):
                result_headers[key] = upstream.headers[key]

        def close():
            upstream.close()
            client.close()

        # Unknown-length sources can still download, but cannot claim verified completion.
        return StreamingResponse(transfer_iterator(job.id, upstream.iter_raw(256 * 1024), start,
                                 end, total, close), status_code=upstream.status_code,
                                 headers=result_headers, media_type='application/octet-stream')
    except Exception:
        if upstream is not None:
            upstream.close()
        client.close()
        raise


@app.get('/api/jobs/{job_id}/file')
def file(job_id: str, request: Request, response: Response, owner_id=Depends(session)):
    job = acquire_transfer(job_id, owner_id)
    try:
        result = cached_response(job, request) if job.filename else direct_response(job, request)
        # Returning a custom Response otherwise drops dependency-set cookies.
        for value in response.headers.getlist('set-cookie'):
            result.headers.append('set-cookie', value)
        return result
    except HTTPException:
        transfer_state(job_id, job.downloaded, None, final=True)
        raise
    except Exception as exc:
        transfer_state(job_id, job.downloaded, None, final=True)
        raise HTTPException(502, 'Не удалось получить видео. Попробуйте продолжить позже.') from exc


@app.head('/api/jobs/{job_id}/file')
def file_head(job_id: str, response: Response, owner_id=Depends(session)):
    with DB() as db:
        job = owned(db, job_id, owner_id)
        if job.status not in ('ready', 'paused', 'streaming'):
            raise HTTPException(409, 'Файл ещё не готов.')
        response.headers['Content-Disposition'] = disposition(job)
        if job.filename:
            path = job_dir(job.id) / job.filename
            if path.parent != job_dir(job.id) or not path.is_file():
                raise HTTPException(410, 'Временный файл больше недоступен.')
            size = path.stat().st_size
            response.headers.update({'Content-Length': str(size), 'Accept-Ranges': 'bytes',
                                     'ETag': f'"{job.id}-{size}-{path.stat().st_mtime_ns}"'})
        else:
            record = json.loads(job.selected)
            for key, header in [('etag', 'ETag'), ('last_modified', 'Last-Modified')]:
                if record.get(key):
                    response.headers[header] = record[key]
            if job.total is not None and (record.get('etag') or record.get('last_modified')):
                response.headers['Content-Length'] = str(job.total)
        response.status_code = 200
        return response


# Registered after shared ownership/session helpers are defined.
from app.browser_routes import router as browser_router
app.include_router(browser_router)
from app.catalog_routes import router as catalog_router
app.include_router(catalog_router)

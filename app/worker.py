"""Persistent DB queue with one supervisor and killable per-job process groups."""
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid

from sqlalchemy import or_, select, update
import yt_dlp

from app.config import MAX_FILE_BYTES, PAUSE_SECONDS, WORKERS
from app.db import DB, Job, init_db
from app.media import inspect, options, refresh
from app.storage import check_space, job_dir, remove_job_files
from app.plans import explain_error
from app.catalog import CatalogFound
from app.player_detection import blank_detection, detect_player, enrich_detection

log = logging.getLogger(__name__)


def change(job_id, **values):
    expected_worker = values.pop('_expected_worker', None)
    if values.get('status') in ('error', 'completed'):
        values.setdefault('history_visible', True)
        values.setdefault('finished_at', time.time())
    with DB.begin() as db:
        stmt = update(Job).where(Job.id == job_id)
        if expected_worker is not None:
            stmt = stmt.where(Job.worker_id == expected_worker)
        if values.get('status') in ('awaiting', 'ready', 'error', 'selection'):
            stmt = stmt.where(Job.status.not_in(['pausing', 'cancelling', 'cancelled', 'paused']))
        db.execute(stmt.values(**values, updated_at=time.time()))


def execute(job_id):
    with DB() as db:
        job = db.get(Job, job_id)
    detection = blank_detection()
    try:
        if job.phase == 'analyze':
            selection = json.loads(job.selected or '{}').get('selection')
            try:
                detection = detect_player(job.url)
                if selection and selection.get('kind') == 'yummy_cvh':
                    detection = {**detection, 'status': 'detected', 'players': ['CVH'],
                                 'note': 'Для выбранной серии используется адаптер CVH.'}
            except Exception:
                # Fingerprinting is best-effort and must never prevent extraction.
                detection = blank_detection()
            change(job_id, selected=json.dumps({'analysis_progress': 0, 'selection': selection,
                                               'player_detection': detection}))
            result = inspect(job.url, job.quality,
                             lambda value: change(job_id, selected=json.dumps({'analysis_progress': value, 'selection': selection,
                                                                             'player_detection': detection})),
                             selection)
            record = json.loads(result['selected'])
            record['player_detection'] = enrich_detection(detection, record=record)
            result['selected'] = json.dumps(record)
            change(job_id, **result, status='awaiting', phase='download', worker_id=None, heartbeat=None,
                   expires_at=time.time() + PAUSE_SECONDS)
            return
        check_space()
        fresh = refresh(job)
        change(job_id, selected=fresh['selected'])
        selected = json.loads(fresh['selected'])
        path = job_dir(job_id)
        path.mkdir(parents=True, exist_ok=True)
        last_update = 0

        def progress(data):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 0.7 and data['status'] != 'finished':
                return
            last_update = now
            check_space()
            size = sum(p.stat().st_size for p in path.glob('*') if p.is_file())
            if size > MAX_FILE_BYTES * 2:
                raise ValueError('Превышен лимит размера временных файлов.')
            # Completed tracks plus current track, without claiming a final 100% before merge.
            downloaded = sum(p.stat().st_size for p in path.glob('*')
                             if p.is_file() and p.suffix not in ('.ytdl', '.json'))
            change(job_id, downloaded=downloaded, speed=data.get('speed'), eta=data.get('eta'),
                   phase='merge' if data['status'] == 'finished' else 'download')

        opts = options() | {
            'format': '+'.join(selected['ids']), 'outtmpl': str(path / 'video.%(ext)s'),
            'merge_output_format': job.extension, 'progress_hooks': [progress],
            'postprocessor_hooks': [lambda _: change(job_id, phase='merge')],
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(job.url, download=True)
        files = [p for p in path.glob('*') if p.is_file() and p.suffix.lstrip('.') in
                 ('mp4', 'webm', 'mkv', 'mov', 'avi', 'flv', 'ts', 'm4v', 'ogv', 'mpeg', 'mpg')
                 and '.f' not in p.stem]
        if len(files) != 1:
            raise ValueError('Не удалось получить один полный видеофайл.')
        output = files[0]
        if output.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('Видео превышает допустимый размер.')
        change(job_id, status='ready', phase='ready', filename=output.name,
               extension=output.suffix.lstrip('.'), downloaded=output.stat().st_size,
               total=output.stat().st_size, speed=None, eta=None,
               expires_at=time.time() + PAUSE_SECONDS, worker_id=None, heartbeat=None)
    except CatalogFound as exc:
        change(job_id, title=exc.catalog['title'], selected=json.dumps({'catalog': exc.catalog,
               'player_detection': enrich_detection(detection, catalog=exc.catalog)}),
               status='selection', phase='select', mode='browser', worker_id=None, heartbeat=None,
               expires_at=time.time() + PAUSE_SECONDS)
    except Exception as exc:
        # Do not expose extractor errors: they may embed source URLs, tokens or network details.
        log.exception('Job %s failed', job_id)
        change(job_id, status='error', error=explain_error(exc), speed=None, eta=None,
               expires_at=time.time() + PAUSE_SECONDS, worker_id=None, heartbeat=None)


def claim(worker_id):
    with DB.begin() as db:
        job = db.scalar(select(Job).where(Job.status.in_(['analyzing', 'queued']),
                        Job.worker_id.is_(None)).order_by(Job.created_at)
                        .with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.worker_id = worker_id
        job.heartbeat = time.time()
        job.status = 'analyzing' if job.phase == 'analyze' else 'downloading'
        return job.id


def cleanup():
    now = time.time()
    with DB.begin() as db:
        db.execute(update(Job).where(Job.status == 'client_downloading', Job.heartbeat < now - 90)
                   .values(status='paused', expires_at=now + PAUSE_SECONDS, speed=None, eta=None))
        # An abandoned transfer keeps its file and becomes resumable.
        db.execute(update(Job).where(Job.status == 'streaming', Job.heartbeat < now - 90)
                   .values(status='paused', expires_at=now + PAUSE_SECONDS, speed=None, eta=None))
        # A crashed worker leaves a lease; wait before reclaiming it.
        db.execute(update(Job).where(Job.worker_id.is_not(None), Job.heartbeat < now - 90,
                                     Job.status == 'cancelling')
                   .values(status='cancelled', worker_id=None, heartbeat=None))
        db.execute(update(Job).where(Job.worker_id.is_not(None), Job.heartbeat < now - 90)
                   .values(status='paused', worker_id=None, heartbeat=None,
                           expires_at=now + PAUSE_SECONDS))
        jobs = db.scalars(select(Job).where(Job.worker_id.is_(None),
                          or_((Job.status == 'cancelled') & Job.data_expired.is_(False), Job.expires_at < now))
                          .with_for_update(skip_locked=True)).all()
        for job in jobs:
            if job.status == 'streaming':
                continue
            try:
                remove_job_files(job.id)
            except OSError:
                continue  # Windows or active readers: retry cleanup later.
            if job.history_visible:
                # Expire media, not the user's history. Do not retain signed stream URLs forever.
                record = json.loads(job.selected or '{}')
                job.selected = json.dumps({'player_detection': record.get('player_detection')})
                job.filename = ''
                job.data_expired = True
                job.expires_at = None
                job.speed = job.eta = None
                if job.status not in ('completed', 'cancelled', 'error'):
                    job.status = 'error'
                    job.error = job.error or 'Срок хранения временных данных истёк. Для новой попытки вставьте ссылку заново.'
                    job.finished_at = now
            else:
                db.delete(job)
    # BrowserSession rows deliberately have no TTL; last_active_at remains available.


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        proc.terminate()
    else:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def main():
    init_db()
    worker_id = str(uuid.uuid4())
    running = {}
    stopping = False

    def shutdown(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    last_cleanup = 0
    try:
        while not stopping:
            for job_id, proc in list(running.items()):
                with DB() as db:
                    job = db.get(Job, job_id)
                if not job or job.status in ('pausing', 'cancelling'):
                    stop_process(proc)
                    if job:
                        change(job_id, status='paused' if job.status == 'pausing' else 'cancelled',
                               expires_at=time.time() + PAUSE_SECONDS, worker_id=None,
                               heartbeat=None, speed=None, eta=None)
                    del running[job_id]
                elif proc.poll() is not None:
                    # Never leave a dead child holding a lease.
                    if job.worker_id == worker_id:
                        change(job_id, status='error', worker_id=None, heartbeat=None, _expected_worker=worker_id,
                               error='Загрузчик завершился. Повторите попытку.',
                               expires_at=time.time() + PAUSE_SECONDS)
                    del running[job_id]
                else:
                    change(job_id, heartbeat=time.time())
            while len(running) < WORKERS:
                job_id = claim(worker_id)
                if not job_id:
                    break
                running[job_id] = subprocess.Popen([sys.executable, '-m', 'app.worker', job_id],
                                                   start_new_session=os.name != 'nt')
            if time.monotonic() - last_cleanup > 10:
                cleanup()
                last_cleanup = time.monotonic()
            time.sleep(0.5)
    finally:
        for job_id, proc in running.items():
            stop_process(proc)
            change(job_id, status='paused', worker_id=None, heartbeat=None,
                   expires_at=time.time() + PAUSE_SECONDS, speed=None, eta=None)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) == 2:
        execute(sys.argv[1])
    else:
        main()

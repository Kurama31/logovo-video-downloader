import shutil

from app.config import DATA_DIR, MAX_DISK_BYTES


def job_dir(job_id):
    # IDs originate in the DB; still enforce a single path component.
    if not job_id or any(c not in 'abcdef0123456789-' for c in job_id):
        raise ValueError('Invalid job ID')
    return DATA_DIR / job_id


def usage():
    return sum(p.stat().st_size for p in DATA_DIR.glob('**/*') if p.is_file())


def check_space():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if usage() >= MAX_DISK_BYTES or shutil.disk_usage(DATA_DIR).free < 256 * 1024**2:
        raise ValueError('Временное хранилище заполнено. Попробуйте позже.')


def remove_job_files(job_id):
    path = job_dir(job_id)
    if path.exists():
        shutil.rmtree(path)

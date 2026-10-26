import os
from pathlib import Path

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./downloads.db')
DATA_DIR = Path(os.getenv('DATA_DIR', './data')).resolve()
PROXY_URL = os.getenv('PROXY_URL', '')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
PUBLIC_ORIGIN = os.getenv('PUBLIC_ORIGIN', 'http://localhost:8080').rstrip('/')
MAX_FILE_BYTES = int(os.getenv('MAX_FILE_BYTES', str(5 * 1024**3)))
MAX_DISK_BYTES = int(os.getenv('MAX_DISK_BYTES', str(30 * 1024**3)))
MAX_SESSION_JOBS = int(os.getenv('MAX_SESSION_JOBS', '10'))
WORKERS = int(os.getenv('DOWNLOAD_WORKERS', '2'))
PAUSE_SECONDS = int(os.getenv('PAUSE_SECONDS', str(7 * 86400)))
READY_SECONDS = int(os.getenv('READY_SECONDS', '3600'))
SESSION_COOKIE = 'dav_session'
WEB_DIR = Path(__file__).parent / 'web'

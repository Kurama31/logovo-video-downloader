import time
import uuid

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {})
DB = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class BrowserSession(Base):
    __tablename__ = 'browser_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_active_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey('browser_sessions.id'), index=True)
    url: Mapped[str] = mapped_column(Text)
    quality: Mapped[int] = mapped_column(default=1080)
    status: Mapped[str] = mapped_column(String(24), default='analyzing', index=True)
    phase: Mapped[str] = mapped_column(String(24), default='analyze')
    title: Mapped[str] = mapped_column(Text, default='Анализ ссылки…')
    selected: Mapped[str] = mapped_column(Text, default='{}')
    mode: Mapped[str] = mapped_column(String(16), default='')
    extension: Mapped[str] = mapped_column(String(16), default='')
    actual_quality: Mapped[int | None] = mapped_column(nullable=True)
    downloaded: Mapped[int] = mapped_column(BigInteger, default=0)
    total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str] = mapped_column(Text, default='')
    filename: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat: Mapped[float | None] = mapped_column(Float, nullable=True)
    history_visible: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('FALSE'))
    data_expired: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('FALSE'))
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class SessionPreference(Base):
    __tablename__ = 'session_preferences'
    owner_id: Mapped[str] = mapped_column(ForeignKey('browser_sessions.id'), primary_key=True)
    destination_mode: Mapped[str] = mapped_column(String(16), default='browser')
    destination_label: Mapped[str] = mapped_column(String(255), default='Папка загрузок браузера')
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


def init_db():
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(8273101)'))
        Base.metadata.create_all(connection)
        columns = {column['name'] for column in inspect(connection).get_columns('jobs')}
        if 'history_visible' not in columns:
            connection.execute(text('ALTER TABLE jobs ADD COLUMN history_visible BOOLEAN NOT NULL DEFAULT FALSE'))
            connection.execute(text("UPDATE jobs SET history_visible = TRUE WHERE status IN ('pending','paused','error','completed','client_downloading','downloading','streaming','queued','ready')"))
        if 'data_expired' not in columns:
            connection.execute(text('ALTER TABLE jobs ADD COLUMN data_expired BOOLEAN NOT NULL DEFAULT FALSE'))
        if 'finished_at' not in columns:
            connection.execute(text('ALTER TABLE jobs ADD COLUMN finished_at FLOAT'))
            connection.execute(text("UPDATE jobs SET finished_at = updated_at WHERE status IN ('completed','error','cancelled')"))

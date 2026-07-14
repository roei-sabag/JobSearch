"""
db/base.py
----------
SQLAlchemy 2.0 async engine + session factory.

Uses SQLite (via aiosqlite) for zero-setup local development. Because all
DB access goes through the async ORM API (AsyncSession, async engine), the
only change needed to move to PostgreSQL later is swapping DATABASE_URL to
an asyncpg connection string (e.g. "postgresql+asyncpg://user:pass@host/db")
-- no model or query code needs to change.
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

WORKDIR = Path(__file__).resolve().parent.parent
DATABASE_URL = f"sqlite+aiosqlite:///{(WORKDIR / 'jobs.db').as_posix()}"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""
    pass


async def get_session() -> AsyncSession:
    """FastAPI dependency: yields a request-scoped AsyncSession."""
    async with AsyncSessionLocal() as session:
        yield session

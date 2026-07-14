"""
db/init_db.py
-------------
Creates all tables in the SQLite database if they don't already exist.
Safe to call on every app startup (idempotent via create_all).
"""

from db.base import engine, Base
from db import models  # noqa: F401  (ensures models are registered on Base.metadata)


async def init_models():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    import asyncio

    asyncio.run(init_models())
    print("[init_db] Tables created (or already existed) in jobs.db")

"""
api/deps.py
-----------
Shared FastAPI dependencies (currently just the DB session).
"""

from db.base import get_session

# Re-exported so routers can do: `from api.deps import get_session`
__all__ = ["get_session"]

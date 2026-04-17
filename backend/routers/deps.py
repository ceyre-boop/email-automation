"""Shared FastAPI dependencies."""
from __future__ import annotations

from typing import Generator

from sqlalchemy.orm import Session

from backend.models.db import get_session_factory


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

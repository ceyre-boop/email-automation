"""Shared FastAPI dependencies."""
from __future__ import annotations

from typing import Generator

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models.db import get_session_factory

_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


def verify_api_key(key: str | None = Security(_api_key_header)) -> None:
    """Reject requests that don't supply the correct x-api-key header."""
    expected = get_settings().api_key
    if not expected:
        # API_KEY not configured — fail open with a warning rather than locking
        # everyone out during initial setup. Set API_KEY in env to enforce auth.
        return
    if key != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API key.",
        )


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

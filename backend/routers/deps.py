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
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfigured — API_KEY env var not set.",
        )
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

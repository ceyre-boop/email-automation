"""Sheets auth circuit breaker.

The Master Log refresh token died around 2026-08-13. Every email since paid for
a failed OAuth round-trip to Google — several per talent per poll cycle — and
logged an identical ERROR line each time, burying every other signal in the logs.
Retrying a dead credential is pure waste: the answer cannot change until a human
replaces the token.
"""
from unittest.mock import patch

import pytest

from backend.services import sheets


@pytest.fixture(autouse=True)
def _reset_breaker():
    sheets._auth_failures = 0
    sheets._breaker_opened_at = None
    sheets._breaker_credential_fp = None
    sheets._suppressed_since_open = 0
    yield
    sheets._auth_failures = 0
    sheets._breaker_opened_at = None


def _log(**kw):
    return sheets.log_email("Allee", "b@x.com", "Subj", 3, "Nike", 100.0, "Sponsored Post", "drafted", **kw)


def _auth_error():
    return Exception("('invalid_grant: Bad Request', {'error': 'invalid_grant'})")


def test_breaker_opens_after_repeated_auth_failures_and_stops_calling_google():
    with patch.object(sheets, "_get_worksheet", side_effect=_auth_error()) as ws:
        for _ in range(sheets._AUTH_FAILURE_LIMIT):
            assert _log() is False
        assert ws.call_count == sheets._AUTH_FAILURE_LIMIT

        # Breaker is now open: further calls must not reach the network at all.
        for _ in range(20):
            assert _log() is False
        assert ws.call_count == sheets._AUTH_FAILURE_LIMIT

    state = sheets.breaker_state()
    assert state["open"] is True
    assert state["suppressed_calls"] == 20


def test_a_transient_api_error_does_not_open_the_breaker():
    """A 500 from Sheets can succeed next time — only credential failures are terminal."""
    with patch.object(sheets, "_get_worksheet", side_effect=Exception("APIError: 503 backend error")):
        for _ in range(10):
            assert _log() is False
    assert sheets.breaker_state()["open"] is False


def test_replacing_the_credential_closes_the_breaker_without_a_redeploy():
    with patch.object(sheets, "_get_worksheet", side_effect=_auth_error()):
        for _ in range(sheets._AUTH_FAILURE_LIMIT):
            _log()
    assert sheets.breaker_state()["open"] is True

    with patch.object(sheets, "_credential_fingerprint", return_value="a-new-token"):
        assert sheets._breaker_is_open() is False
    assert sheets.breaker_state()["open"] is False


def test_success_resets_the_failure_count():
    with patch.object(sheets, "_get_worksheet", side_effect=_auth_error()):
        _log()
    assert sheets._auth_failures == 1

    with patch.object(sheets, "_get_worksheet") as ws:
        ws.return_value.append_row.return_value = None
        assert _log() is True
    assert sheets._auth_failures == 0


def test_auth_error_detection_covers_the_real_google_payloads():
    for msg in ("invalid_grant: Bad Request", "invalid_client: bad secret",
                "unauthorized_client", "Token has been expired or revoked."):
        assert sheets._is_auth_error(Exception(msg)) is True
    assert sheets._is_auth_error(Exception("Quota exceeded for writes")) is False

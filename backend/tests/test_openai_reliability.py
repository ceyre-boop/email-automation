"""Timeout + retry policy for the OpenAI calls.

Two real gaps, from the 2026-09-04 audit:
  - neither triage nor reply set a request timeout, so a hung call blocked a
    poller worker (holding a DB session) indefinitely while APScheduler still
    logged the job as executed successfully;
  - reply.py had no retry at all, so one transient 429 escalated a genuine brand
    deal to human review — revenue lost silently, invisible in every metric.
"""
from unittest.mock import MagicMock, patch

import pytest

from backend.services.openai_client import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    build_client,
    call_with_retry,
    is_transient,
)


@pytest.mark.parametrize("message", [
    "Error code: 429 - rate_limit_exceeded",
    "Error code: 503 - service unavailable",
    "Error code: 500 - InternalServerError",
    "Request timed out.",
    "APIConnectionError: connection reset by peer",
])
def test_transient_failures_are_retryable(message):
    assert is_transient(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "Error code: 429 - You exceeded your current quota",
    "Rate limit reached for requests per day (RPD)",
    "insufficient_quota: billing hard limit reached",
    "Error code: 400 - invalid_request_error",
    "Error code: 401 - invalid api key",
])
def test_hard_caps_and_client_errors_are_not_retried(message):
    """A daily cap will not clear before midnight — burning 35s of backoff on it
    just slows the queue. A 400/401 will never succeed on retry either."""
    assert is_transient(Exception(message)) is False


def test_client_is_built_with_a_timeout_and_no_sdk_retries():
    with patch("backend.services.openai_client.OpenAI") as mock_openai:
        build_client("sk-test")
    kwargs = mock_openai.call_args.kwargs
    assert kwargs["timeout"] == DEFAULT_TIMEOUT_SECONDS
    assert kwargs["max_retries"] == 0  # our loop is the only retry layer


def test_custom_timeout_is_honoured():
    with patch("backend.services.openai_client.OpenAI") as mock_openai:
        build_client("sk-test", 12.5)
    assert mock_openai.call_args.kwargs["timeout"] == 12.5


def test_transient_failure_is_retried_then_succeeds():
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("Error code: 429 - rate_limit_exceeded"),
        "ok",
    ]
    with patch("backend.services.openai_client.time.sleep") as sleep:
        assert call_with_retry(client, "Allee") == "ok"
    assert client.chat.completions.create.call_count == 2
    sleep.assert_called_once_with(5)


def test_backoff_is_exponential_and_bounded_by_max_attempts():
    client = MagicMock()
    client.chat.completions.create.side_effect = Exception("503 service unavailable")
    with patch("backend.services.openai_client.time.sleep") as sleep:
        with pytest.raises(Exception, match="503"):
            call_with_retry(client, "Allee")
    assert client.chat.completions.create.call_count == DEFAULT_MAX_ATTEMPTS
    assert [c.args[0] for c in sleep.call_args_list] == [5, 10]  # no sleep after the last try


def test_non_transient_error_fails_immediately_without_sleeping():
    client = MagicMock()
    client.chat.completions.create.side_effect = Exception("400 invalid_request_error")
    with patch("backend.services.openai_client.time.sleep") as sleep:
        with pytest.raises(Exception, match="400"):
            call_with_retry(client, "Allee")
    assert client.chat.completions.create.call_count == 1
    sleep.assert_not_called()


def test_max_attempts_is_configurable():
    client = MagicMock()
    client.chat.completions.create.side_effect = Exception("429 rate_limit_exceeded")
    with patch("backend.services.openai_client.time.sleep"):
        with pytest.raises(Exception):
            call_with_retry(client, "Allee", max_attempts=2)
    assert client.chat.completions.create.call_count == 2

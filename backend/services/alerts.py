"""Out-of-band alerting and liveness pings.

Email alerts go out through a talent's own Gmail token, so they die exactly when a
token dies — the failure we most need to hear about. Two optional env vars add
channels that do not depend on any talent's Gmail:

  ALERT_WEBHOOK_URL  Slack-compatible incoming webhook (JSON POST {"text": ...}).
                     Guardian and stall-alarm alerts post here in addition to email.
                     If unset, a warning is logged once and only email is used.
  HEARTBEAT_URL      healthchecks.io-style dead-man's-switch URL. Pinged (GET) only
                     after a poll cycle completes without raising; if polling stops
                     or crashes the pings stop and the external service alerts.

Both helpers never raise: alerting must not be able to break the job it reports on.
"""
from __future__ import annotations

import logging

import httpx

from backend.core.config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0
_webhook_warned = False


def _webhook_url() -> str:
    return (get_settings().alert_webhook_url or "").strip()


def _heartbeat_url() -> str:
    return (get_settings().heartbeat_url or "").strip()


def post_alert_webhook(title: str, body: str = "") -> bool:
    """POST an alert to ALERT_WEBHOOK_URL. Returns True if delivered (2xx)."""
    global _webhook_warned
    url = _webhook_url()
    if not url:
        if not _webhook_warned:
            _webhook_warned = True
            logger.warning(
                "ALERT_WEBHOOK_URL is not set — alerts go to email only, which depends on a "
                "talent's Gmail token being alive. Set it to a Slack-compatible webhook."
            )
        return False
    text = f"*{title}*" + (f"\n{body}" if body else "")
    try:
        resp = httpx.post(url, json={"text": text[:3500]}, timeout=_TIMEOUT_SECONDS)
        if resp.status_code >= 300:
            logger.error("Alert webhook returned HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Alert webhook post failed: %s", exc)
        return False


def ping_heartbeat() -> bool:
    """GET HEARTBEAT_URL if configured. Returns True if pinged OK."""
    url = _heartbeat_url()
    if not url:
        return False
    try:
        resp = httpx.get(url, timeout=_TIMEOUT_SECONDS)
        if resp.status_code >= 300:
            logger.warning("Heartbeat ping returned HTTP %s", resp.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Heartbeat ping failed: %s", exc)
        return False

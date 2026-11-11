#!/usr/bin/env python3
"""Standalone, fixture-only preview server for backend/static/dashboard.html.

Why this exists: running the real app locally connects to the production
database and starts the scheduler, which polls real inboxes and auto-sends.
This server imports nothing from `backend`, opens no database, makes no network
calls and talks to no Google/OpenAI API. It serves the dashboard HTML and answers
every API path the dashboard calls with fake JSON; every POST is a no-op that
returns success.

Usage:
    python scripts/preview_dashboard.py                      # http://localhost:8765/
    python scripts/preview_dashboard.py --port 8765 --scenario stalled

Scenarios change what GET /health reports so each banner state can be reviewed:
    dead-token (default)  one disconnected Gmail, AI on
    healthy               everything green
    stalled               pipeline stalled + dead token
    schema                schema_ok = false
    ai-off                AI disabled
    all                   dead token + stalled + schema + AI off

The dashboard asks for an API key on first load; this server accepts any
non-empty value (it is never checked against anything). Open
http://localhost:8765/?autologin=1 to skip the prompt.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DASHBOARD_HTML = Path(__file__).resolve().parent.parent / "backend" / "static" / "dashboard.html"
SCENARIOS = ("dead-token", "healthy", "stalled", "schema", "ai-off", "all")


def _ago(**kw) -> str:
    """Naive-UTC ISO string like the real API returns."""
    return (datetime.now(timezone.utc) - timedelta(**kw)).replace(tzinfo=None).isoformat()


# ── Fixtures ──────────────────────────────────────────────────────────────────

TALENTS = [
    # key, full_name, manager, connected, auto_send, draft_mode
    ("Jocelyn", "Jocelyn Rivera", "Cara", True, True, False),
    ("Maya", "Maya Chen", "Chenni", True, False, True),
    ("Devon", "Devon Brooks", "Nicole", True, False, True),
    ("Priya", "Priya Natarajan", "Cara", True, True, False),
    ("Katrina", "Katrina Ortiz", "Chenni", False, False, True),
]

# talent_key -> (good, review, trash, pending drafts, best brand, best rate)
REPORT_ROWS = {
    "Jocelyn": (14, 6, 41, 3, "Glow Recipe", 1800),
    "Maya": (9, 11, 52, 2, "Fenty Skin", 1200),
    "Devon": (5, 4, 33, 1, "Gymshark", 900),
    "Priya": (11, 8, 47, 0, "Tatcha", 2400),
    "Katrina": (0, 2, 12, 0, None, None),
}


def talents_payload():
    return [
        {
            "key": k, "full_name": n, "manager": m, "category": "Beauty",
            "minimum_rate_usd": 500.0, "connected": c,
            "email": f"{k.lower()}@example.test" if c else None,
            "inbox_email": "talent-mgmt@example.test" if c else None,
            "connected_at": _ago(days=40) if c else None,
            "paused": False, "draft_mode": dm, "auto_send": a,
        }
        for k, n, m, c, a, dm in TALENTS
    ]


def report_payload():
    cards = []
    for k, n, m, *_ in TALENTS:
        good, rev, trash, pend, brand, rate = REPORT_ROWS[k]
        cards.append({
            "talent_key": k, "full_name": n, "manager": m,
            "count_good": good, "count_uncertain": rev, "count_trash": trash,
            "count_sent": 2, "count_drafts": pend, "new_drafts_today": pend,
            "count_ignore": trash, "total": good + rev + trash,
            "best_deal_brand": brand, "best_deal_rate": rate,
            "pending_drafts": pend, "pending_real_drafts": pend,
        })
    return {
        "report_date": datetime.now().strftime("%b %d, %Y"),
        "total_good": 39, "total_uncertain": 31, "total_trash": 185, "total_emails": 255,
        "total_sent": 9, "total_draft_backlog": 6, "total_new_drafts_today": 6,
        "total_ignore": 185, "total_deal_value_today": 6300.0,
        "total_sent_cal_today": 7, "total_new_drafts_cal_today": 6,
        "total_revisit_cal_today": 52, "total_replies_today": 4,
        "talents": cards,
    }


def _draft(id_, talent, sender, subject, brand, rate, text, minutes_ago, **kw):
    d = {
        "id": id_, "talent_key": talent, "gmail_message_id": f"msg-{id_:04d}",
        "sender": sender, "subject": subject, "brand_name": brand,
        "proposed_rate": rate, "offer_type": "Sponsored video",
        "triage_reason": kw.pop("triage_reason", "Clear paid offer above the talent's rate floor."),
        "draft_text": text, "status": "pending", "is_escalate": False,
        "escalate_reason": None, "created_at": _ago(minutes=minutes_ago),
        "reviewed_at": None, "triggered_by_job": "draft_queue", "human_edited": False,
        "human_edited_at": None, "human_edited_by": None,
        "validation_failed": False, "validation_error": None,
    }
    d.update(kw)
    return d


def pending_drafts():
    return [
        _draft(101, "Jocelyn", "Sam Lee <sam@glowrecipe.example>", "Spring launch collab — paid TikTok Shop video",
               "Glow Recipe", 1800.0,
               "Hi Sam,\n\nThanks so much for reaching out! Jocelyn would love to be part of the spring launch.\n"
               "Her rate for a dedicated TikTok Shop video is $1,800 with 30-day usage.\n\nBest,\nCara", 7),
        _draft(102, "Jocelyn", "Priyanka R <pr@tatcha.example>", "Gifting + affiliate opportunity",
               "Tatcha", 900.0, "Hi Priyanka,\n\nThank you for the offer. Could you share the affiliate commission rate?\n\nBest,\nCara", 35),
        _draft(103, "Maya", "Alex Kim <alex@fentyskin.example>", "Paid partnership — Hydra Vizor launch",
               "Fenty Skin", 1200.0, "Hi Alex,\n\nMaya is excited about Hydra Vizor. Her rate is $1,200 per video.\n\nBest,\nChenni", 95),
        _draft(104, "Maya", "Dana <dana@brightlabs.example>", "Re: Maya x Bright Labs",
               "Bright Labs", None,
               "(Escalation — needs a human-written reply.)", 200,
               is_escalate=True, escalate_reason="No matching approved response for a usage-rights negotiation.",
               triage_reason="Counter-offer asks for 12-month paid usage."),
        _draft(105, "Maya", "Jo <jo@sunnydays.example>", "Collab request",
               "Sunny Days", 750.0, "Hi Jo,\n\nThanks! Maya's rate is $750.\n\nBest,\nChenni", 410,
               validation_failed=True, validation_error="Draft quotes a rate below the talent minimum."),
        _draft(106, "Devon", "Chris <chris@gymshark.example>", "Gymshark x Devon — spring drop",
               "Gymshark", 900.0, "Hi Chris,\n\nDevon would love to. His rate is $900 per video.\n\nBest,\nNicole", 1500),
    ]


def sent_drafts():
    rows = []
    samples = [
        ("Jocelyn", "Mara <mara@sephora.example>", "Sephora Squad — June campaign", "Sephora", 2200.0, True, 22),
        ("Priya", "Lena <lena@tatcha.example>", "Tatcha holiday set", "Tatcha", 2400.0, True, 70),
        ("Maya", "Omar <omar@skims.example>", "SKIMS creator program", "SKIMS", 1500.0, False, 190),
        ("Devon", "Rae <rae@alo.example>", "Alo Yoga ambassador offer", "Alo Yoga", 1000.0, True, 420),
        ("Jocelyn", "Tess <tess@olaplex.example>", "Olaplex No.3 TikTok Shop push", "Olaplex", 1300.0, False, 1500),
        ("Priya", "Ben <ben@summerfridays.example>", "Summer Fridays x Priya", "Summer Fridays", 1750.0, True, 2900),
    ]
    for i, (talent, sender, subject, brand, rate, auto, mins) in enumerate(samples):
        d = _draft(
            200 + i, talent, sender, subject, brand, rate,
            f"Hi {sender.split()[0]},\n\nThanks for reaching out about {brand}. Our rate for this is ${rate:,.0f}.\n\nBest,\nThe TABOOST team",
            mins + 5,
        )
        d.update(status="sent", reviewed_at=_ago(minutes=mins),
                 triggered_by_job="auto_send" if auto else "manager-dashboard")
        rows.append(d)
    return rows


def email_feed():
    base = [
        ("Jocelyn", "Nova PR <hello@novapr.example>", "Quick question about your availability", 2, "Vague inquiry, no rate or deliverables stated.", False, 12),
        ("Maya", "Riley <riley@pulsebrand.example>", "Re: Maya x Pulse", 2, "Reply on an existing thread — needs human review.", False, 40),
        ("Devon", "Deals Team <deals@bulkoffers.example>", "You've been selected!!!", 1, "Mass-mail giveaway spam.", False, 65),
        ("Priya", "Kai <kai@lumiere.example>", "Lumiere serum — paid video", 3, "Paid offer, draft was lost before it was saved.", True, 120),
        ("Maya", "Growth <growth@clickfarm.example>", "Earn $$$ with our affiliate network", 1, "Generic affiliate network pitch.", False, 180),
        ("Jocelyn", "Taylor <taylor@mintcosmetics.example>", "Mint Cosmetics x Jocelyn", 2, "Wants a call before sharing budget.", False, 260),
    ]
    return [
        {
            "gmail_message_id": f"feed-{i:03d}", "talent_key": t, "sender": s, "subject": subj,
            "score": sc, "triage_reason": why, "is_lost": lost, "processed_at": _ago(minutes=m),
        }
        for i, (t, s, subj, sc, why, lost, m) in enumerate(base)
    ]


def orphaned():
    return [{
        "gmail_message_id": "orph-001", "talent_key": "Priya", "sender": "Kai <kai@lumiere.example>",
        "subject": "Lumiere serum — paid video", "brand_name": "Lumiere", "proposed_rate": 1100.0,
        "offer_type": "Sponsored video", "triage_reason": "Score 3 with no draft attached.",
        "processed_at": _ago(minutes=118),
    }]


def external_review():
    return [{
        "gmail_message_id": "ext-001", "talent_key": "Devon", "channel_requested": "WhatsApp",
        "sender": "Mo <mo@streetwear.example>", "subject": "Let's move this to WhatsApp",
        "body_text": "Hey Devon's team, easier to chat on WhatsApp: +1 555 010 0199. We have a budget ready.",
    }]


def health_payload(scenario: str) -> dict:
    dead = scenario in ("dead-token", "stalled", "all")
    stalled = scenario in ("stalled", "all")
    schema_bad = scenario in ("schema", "all")
    ai_off = scenario in ("ai-off", "all")
    details = [{
        "talent_key": "Katrina", "email": "katrina@example.test",
        "reason": "invalid_grant: Token has been expired or revoked.",
        "deactivated_at": _ago(hours=5, minutes=12), "consecutive_failures": 3,
    }] if dead else []
    return {
        "status": "ok",
        "schema_ok": not schema_bad,
        "schema": {
            "checked_at": _ago(minutes=30),
            "missing_tables": [],
            "missing_columns": ["drafts.send_claimed_at"] if schema_bad else [],
            "missing_enum_values": [], "error": None,
        },
        "ai_enabled": not ai_off,
        "deployed_at": "2026-11-11T18:41:17",
        "sop_hash": "preview", "talent_count": len(TALENTS), "talent_warnings": 0,
        "any_warnings": False, "last_parse_timestamp": _ago(hours=2),
        "sop_version_id": 1, "sop_version_label": "preview", "sop_version_uploaded_at": _ago(days=3),
        "pipeline": {
            "stalled": stalled,
            "reason": ("NOTHING PROCESSED — 0 emails triaged in 30 min while 12 unprocessed unread "
                       "sit in the inbox (14 unread total). Polling or triage is down.") if stalled else None,
            "window_minutes": 30, "processed_in_window": 0 if stalled else 41,
            "dead_tokens": [d["talent_key"] for d in details],
            "dead_token_details": details,
            "paused_talents": [],
        },
        "db_pool": {"checked_out": 1, "capacity": 15, "saturated": False},
        "db_writes": {"per_minute": 12},
    }


def token_health(scenario: str = "dead-token"):
    rows = []
    for k, _n, _m, connected, *_ in TALENTS:
        dead = k == "Katrina" and scenario in ("dead-token", "stalled", "all")
        rows.append({
            "talent_key": k, "email": f"{k.lower()}@example.test",
            "active": connected, "consecutive_failures": 3 if dead else 0,
            "last_error": "invalid_grant: Token has been expired or revoked." if dead else None,
            "deactivated_at": _ago(hours=5, minutes=12) if dead else None, "dead": dead,
            "last_poll_at": _ago(minutes=2) if connected else None,
            "token_expiry": _ago(minutes=-40),
        })
    return rows


def analytics(path: str):
    if path.endswith("/operational-load"):
        days = [(datetime.now() - timedelta(days=6 - i)).strftime("%Y-%m-%d") for i in range(7)]
        return {"total_emails": 255, "automation_rate": 0.87, "estimated_minutes_saved": 410,
                "human_overrides": 6, "daily_volume": [{"date": d, "count": c} for d, c in zip(days, [210, 245, 190, 260, 232, 281, 255])]}
    if path.endswith("/talent-health"):
        return [
            {"talent_key": k, "full_name": n, "total_emails": g + r + t, "score3_deals": g,
             "score2_review": r, "score1_spam": t, "avg_risk_score": 1.0 + (i % 4)}
            for i, (k, n, _m, *_x) in enumerate(TALENTS)
            for g, r, t, *_ in [REPORT_ROWS[k]]
        ]
    if path.endswith("/scenario-performance"):
        return [
            {"offer_type": "Sponsored video", "count": 88, "score3_pct": 0.42, "avg_rate_usd": 1350},
            {"offer_type": "Gifting only", "count": 64, "score3_pct": 0.03, "avg_rate_usd": 0},
            {"offer_type": "Affiliate", "count": 51, "score3_pct": 0.12, "avg_rate_usd": 420},
            {"offer_type": "Event invite", "count": 17, "score3_pct": 0.0, "avg_rate_usd": 0},
        ]
    if path.endswith("/anomalies"):
        return {"anomalies": [
            {"severity": "warning", "talent_key": "Maya", "message": "Review queue grew 3x over the 7-day average."},
            {"severity": "info", "talent_key": None, "message": "Automation rate is steady at 87%."},
        ]}
    if "/triage-intelligence" in path:
        return {"total": 255, "score3_count": 39, "score2_count": 31, "score1_count": 185, "fallback_count": 0,
                "top_score2_reasons": [{"count": 9, "reason": "Reply on an existing thread — needs human review."},
                                       {"count": 6, "reason": "Vague inquiry, no rate stated."}]}
    if path.endswith("/marco/messages"):
        return [{"id": 1, "severity": "info", "category": "volume", "talent_key": None,
                 "message": "Volume is in line with last week. No action needed.", "created_at": _ago(hours=1)},
                {"id": 2, "severity": "warning", "category": "tokens", "talent_key": "Katrina",
                 "message": "Katrina's Gmail has been disconnected for over 5 hours.", "created_at": _ago(hours=4)}]
    if "/email-feed" in path:
        return email_feed()
    return None


def health_score():
    return {"score": 0.93, "status": "healthy",
            "components": {"triage_reliability": 1.0, "queue_liveness": 0.98, "draft_freshness": 0.95,
                           "token_health": 0.8, "poll_health": 1.0},
            "issues": ["1 Gmail token is disconnected (Katrina)."]}


SOP_HTML = ("<h2>Standard Operating Procedures (preview)</h2><h3>Rate floors</h3>"
            "<p>This is fixture text — the real SOP is not loaded in preview mode.</p>")

# POST paths that need a specific response shape; everything else gets the default.
POST_SHAPES = [
    (r"/api/dashboard/recover-fallbacks$", {"deleted_fallback_records": 0, "queued_talents": []}),
    (r"/api/drafts/discard-stale$", {"matched": 0, "discarded": 0, "skipped_send_in_progress": 0}),
    (r"/api/drafts/orphaned/trash-all$", {"trashed": 0, "failed": 0}),
    (r"/api/analytics/marco/generate$", {"generated": 0}),
    (r"/api/analytics/marco/dismiss-all$", {"dismissed": 0}),
    (r"/api/dashboard/admin/revalidate-drafts$", {"cleared": 0, "still_failed": 0}),
    (r"/api/analytics/retriage-backfill$", {"released": 0}),
    (r"/api/dashboard/admin/drafts/purge-stale$", {"deleted": 0, "message": "preview: nothing was deleted"}),
    (r"/api/dashboard/talents/[^/]+/send-all$", {"sent_count": 0, "failed_count": 0}),
    (r"/api/drafts/\d+/approve$", {"ok": True, "status": "sent"}),
]


# ── Server ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    scenario = "dead-token"
    server_version = "DashboardPreview/1.0"

    def log_message(self, fmt, *args):  # quieter, single-line log
        print(f"[preview] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # helpers
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _authed(self) -> bool:
        # The real API 403s without a key; any non-empty value is accepted here.
        return bool(self.headers.get("x-api-key", "").strip())

    # GET
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path.rstrip("/") or "/", parse_qs(u.query)

        if path == "/":
            try:
                html = DASHBOARD_HTML.read_bytes()
            except OSError as exc:
                return self._send(500, f"cannot read dashboard.html: {exc}".encode(), "text/plain")
            if qs.get("autologin"):
                # Convenience only: skip the API-key prompt (the key is never checked).
                html = html.replace(b"<head>", b"<head><script>sessionStorage.setItem('inbox_api_key','preview')</script>", 1)
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/health":
            return self._json(health_payload(self.scenario))
        if path == "/auth/connect":
            key = (qs.get("talent_key") or ["?"])[0]
            page = (f"<h1>Preview</h1><p>Gmail reconnect for <b>{key}</b> would start here. "
                    "Nothing is connected in preview mode.</p>")
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self._json({"detail": "Not found"}, 404)
        if not self._authed():
            return self._json({"detail": "Invalid API key"}, 403)

        if path == "/api/dashboard/report":
            return self._json(report_payload())
        if path == "/api/dashboard/talents":
            return self._json(talents_payload())
        if path == "/api/dashboard/health/tokens":
            return self._json(token_health(self.scenario))
        if path == "/api/dashboard/health/score":
            return self._json(health_score())
        if path == "/api/dashboard/sop-html":
            return self._json({"html": SOP_HTML})
        if path == "/api/dashboard/external-channel-review":
            return self._json(external_review())
        if path == "/api/admin/guardian/status":
            return self._json({"ai_enabled": self.scenario not in ("ai-off", "all"), "guardian_enabled": True})
        if path == "/api/drafts":
            status = (qs.get("status") or [None])[0]
            rows = sent_drafts() if status == "sent" else pending_drafts() if status in (None, "pending") else []
            limit = (qs.get("limit") or [None])[0]
            return self._json(rows[: int(limit)] if limit and limit.isdigit() else rows)
        if path == "/api/drafts/orphaned":
            return self._json(orphaned())
        if path.startswith("/api/analytics/"):
            data = analytics(path)
            if data is not None:
                return self._json(data)
        return self._json({"detail": f"preview has no fixture for GET {path}"}, 404)

    # POST / PUT / DELETE: harmless no-ops
    def _noop(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # drain; never stored or acted on
        path = urlparse(self.path).path.rstrip("/")
        if not path.startswith("/api/"):
            return self._json({"detail": "Not found"}, 404)
        if not self._authed():
            return self._json({"detail": "Invalid API key"}, 403)
        for pattern, shape in POST_SHAPES:
            if re.search(pattern, path):
                return self._json({"ok": True, "preview": True, **shape})
        return self._json({"ok": True, "preview": True, "message": "no-op (preview mode)"})

    do_POST = do_PUT = do_DELETE = _noop


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scenario", choices=SCENARIOS, default="dead-token")
    args = ap.parse_args()
    Handler.scenario = args.scenario
    # Loopback only: this is a local review tool, never expose it.
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[preview] dashboard preview (fixtures only, no DB) on http://localhost:{args.port}/  scenario={args.scenario}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

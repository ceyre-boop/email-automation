"""Guards the automatic Supabase session→transaction pooler port rewrite.

Session mode (5432) caps this project at ~15 concurrent clients and refuses
connects past that ("EMAXCONNSESSION"), which killed whole poll cycles in
production. Transaction mode (6543) multiplexes and lifts the ceiling.
"""
import pytest

from backend.models.db import _resolve_pooler_url

POOLER = "aws-1-us-east-2.pooler.supabase.com"


def test_session_port_is_rewritten_to_transaction_port():
    url, txn = _resolve_pooler_url(f"postgresql://postgres.ref:pw@{POOLER}:5432/postgres")
    assert url == f"postgresql://postgres.ref:pw@{POOLER}:6543/postgres"
    assert txn is True


def test_transaction_port_is_left_alone_and_query_preserved():
    original = f"postgresql://postgres.ref:pw@{POOLER}:6543/postgres?sslmode=require"
    url, txn = _resolve_pooler_url(original)
    assert url == original
    assert txn is True


def test_encoded_password_is_not_mangled():
    url, _ = _resolve_pooler_url(f"postgresql://postgres.ref:p%40ss%2Fwd@{POOLER}:5432/postgres")
    assert "p%40ss%2Fwd" in url


def test_missing_port_on_pooler_host_defaults_to_transaction():
    url, txn = _resolve_pooler_url(f"postgresql://u:p@{POOLER}/postgres")
    assert url.endswith(":6543/postgres")
    assert txn is True


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres:pw@db.ref.supabase.co:5432/postgres",  # direct, not pooled
        "postgresql://u:p@localhost:5432/app",
        "not a url",
    ],
)
def test_non_pooler_urls_are_untouched(url):
    assert _resolve_pooler_url(url) == (url, False)


def test_opt_out_env_keeps_session_mode(monkeypatch):
    monkeypatch.setenv("DB_POOLER_MODE", "session")
    original = f"postgresql://postgres.ref:pw@{POOLER}:5432/postgres"
    assert _resolve_pooler_url(original) == (original, False)


def test_unexpected_port_is_not_rewritten():
    original = f"postgresql://postgres.ref:pw@{POOLER}:5433/postgres"
    assert _resolve_pooler_url(original) == (original, False)

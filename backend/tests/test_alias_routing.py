"""Alias routing from message headers (SOP v16 Rule 12).

Production reality these guard against, taken from live unrouted mail:
brands BCC the talent alias and put their OWN address in `To`, so the alias
exists only in Delivered-To/Bcc. Trusting `To` sent every one of those to the
unrouted pile — 220 unrouted vs 1 drafted in a 20-minute window.
"""
from backend.services.gmail import get_to_address

ALIAS_MAP = {
    "allee@taboost.me": "Allee",
    "angela@taboost.me": "Angela",
    "wesley@taboost.me": "Wesley",
    "katrina@taboost.me": "Katrina",
}


def d(**headers):
    return {"headers": headers}


def test_bcc_blast_resolves_from_delivered_to_not_the_senders_own_to():
    detail = d(**{
        "delivered-to": "angela@taboost.me",
        "to": "chain Bee <beechain@beelinkmedia.com>",
        "bcc": "angela@taboost.me",
    })
    assert get_to_address(detail, ALIAS_MAP) == "angela@taboost.me"


def test_alias_later_in_a_multi_recipient_header_is_still_found():
    detail = d(to="TABOOST Talent <talent-mgmt@taboost.me>, Katrina <katrina@taboost.me>")
    assert get_to_address(detail, ALIAS_MAP) == "katrina@taboost.me"


def test_alias_in_cc_only():
    detail = d(to="brand@example.com", cc="someone@else.com, allee@taboost.me")
    assert get_to_address(detail, ALIAS_MAP) == "allee@taboost.me"


def test_x_original_to_wins_over_later_headers():
    detail = d(**{
        "x-original-to": "wesley@taboost.me",
        "delivered-to": "allee@taboost.me",
    })
    assert get_to_address(detail, ALIAS_MAP) == "wesley@taboost.me"


def test_delivered_to_beats_display_to_when_both_are_aliases():
    detail = d(**{"delivered-to": "wesley@taboost.me", "to": "allee@taboost.me"})
    assert get_to_address(detail, ALIAS_MAP) == "wesley@taboost.me"


def test_mail_addressed_only_to_the_shared_inbox_stays_unrouted():
    # Genuinely has no alias — correct behaviour is None, left for human review.
    detail = d(**{
        "delivered-to": "talent-mgmt@taboost.me",
        "to": "TABOOST Talent <talent-mgmt@taboost.me>",
    })
    assert get_to_address(detail, ALIAS_MAP) is None


def test_unknown_alias_falls_back_to_first_non_inbox_address_for_logging():
    detail = d(**{"delivered-to": "talent-mgmt@taboost.me", "to": "stranger@example.com"})
    assert get_to_address(detail, ALIAS_MAP) == "stranger@example.com"


def test_without_alias_map_keeps_legacy_first_non_inbox_behaviour():
    detail = d(**{"delivered-to": "angela@taboost.me", "to": "brand@example.com"})
    assert get_to_address(detail) == "angela@taboost.me"


def test_no_headers_at_all():
    assert get_to_address({}, ALIAS_MAP) is None


def test_case_and_whitespace_insensitive():
    detail = d(**{"delivered-to": "  ANGELA@TaBoost.me "})
    assert get_to_address(detail, ALIAS_MAP) == "angela@taboost.me"

"""SOP v16 Part 3 — Reply-To Routing List.

Part 3 defines three groups keyed on the TALENT INBOX. After the v16
consolidation a talent's inbox is the ALIAS the brand emailed, not the mailbox
that received it — 13 talents share talent-mgmt@taboost.me. Resolving on the
receiving token alone (the pre-fix behaviour) matched only the four Partnerships
talents and left the other 13 with no Reply-To at all, so the five
creator-mgmt@ talents had brand replies routed to the wrong mailbox.

Deviating from Part 3 is an SOP violation, so these tests read the group
membership out of sheets/sop.md itself rather than restating it.
"""
import re
from pathlib import Path

import pytest

from backend.core.config import get_settings
from backend.services.inbox_routing import reply_to_for_talent, reply_to_groups

SOP_PATH = Path(__file__).resolve().parents[2] / "sheets" / "sop.md"


def _sop_part3_groups() -> dict[str, list[str]]:
    text = SOP_PATH.read_text(encoding="utf-8")
    part3 = text[text.index("Part 3 — Reply-To Routing List"):text.index("Part 4")]
    groups: dict[str, list[str]] = {}
    current = None
    for line in part3.splitlines():
        line = line.strip()
        m = re.match(r"Use Reply-To:\s*(\S+@\S+)", line)
        if m:
            current = m.group(1).lower()
            groups.setdefault(current, [])
            continue
        m = re.match(r"-\s*(.+?)\s*/\s*(\S+@\S+)\s*$", line)
        if m and current:
            groups[current].append(m.group(2).lower())
    return groups


# The creator-mgmt@taboost.me mailbox was retired. Its five talents are
# deliberately listed in NO Part 3 group, which per Part 3's closing line means
# blank/default Reply-To — the behaviour they already had. Adding a group back
# for a dead mailbox would drop brand replies entirely.
RETIRED_GROUPS = {"creator-mgmt@taboost.me"}
NO_REPLY_TO_BY_DESIGN = {
    "mahogany@taboost.me", "anastasiya@taboost.me", "jenn@taboost.me",
    "grayson@taboost.me", "bkuhl@taboost.me",
}


def test_config_mirrors_sop_part3_exactly():
    """settings.json is a mirror of Part 3 — any drift is an SOP violation."""
    sop = {addr: sorted(m) for addr, m in _sop_part3_groups().items()}
    cfg = {addr: sorted(m) for addr, m in reply_to_groups().items()}
    assert cfg == sop


def test_retired_group_appears_in_neither_sop_nor_config():
    """Guards against a dead mailbox being reintroduced to either file."""
    assert not (set(reply_to_groups()) & RETIRED_GROUPS)
    assert not (set(_sop_part3_groups()) & RETIRED_GROUPS)


def test_live_groups_are_present():
    assert set(reply_to_groups()) == {
        "talent-mgmt@taboost.me",
        "partnerships@taboost.me",
    }


@pytest.mark.parametrize("talent_key,expected", [
    # talent-mgmt — previously resolved to None because the token is the shared inbox
    ("Allee", "talent-mgmt@taboost.me"),
    ("Lizz", "talent-mgmt@taboost.me"),
    ("Angela", "talent-mgmt@taboost.me"),
    ("Alana", "talent-mgmt@taboost.me"),
    ("Stephanie", "talent-mgmt@taboost.me"),
    ("Jocelyn", "talent-mgmt@taboost.me"),
    ("Hana", "talent-mgmt@taboost.me"),
    ("Wesley", "talent-mgmt@taboost.me"),
])
def test_shared_inbox_talents_resolve_by_alias(talent_key, expected):
    class Token:  # the shared mailbox — in no Part 3 group itself
        email = "talent-mgmt@taboost.me"
    assert reply_to_for_talent(talent_key, token_row=Token()) == expected


def test_talent_key_is_case_insensitive():
    assert reply_to_for_talent("allee") == "talent-mgmt@taboost.me"
    assert reply_to_for_talent("ALLEE") == "talent-mgmt@taboost.me"


@pytest.mark.parametrize("talent_key", ["Mahogany", "Anastasiya", "Jenn", "Grayson", "BKuhl"])
def test_retired_creator_mgmt_talents_get_no_reply_to(talent_key):
    """Unchanged from current live behaviour, and deliberately so — a Reply-To
    pointing at the retired creator-mgmt@ mailbox would lose the reply outright."""
    class Token:
        email = "talent-mgmt@taboost.me"
    assert reply_to_for_talent(talent_key, token_row=Token()) is None


def test_partnerships_talents_still_resolve_from_their_own_token():
    class Token:
        email = "katrina@taboost.me"
    assert reply_to_for_talent("Katrina", token_row=Token()) == "partnerships@taboost.me"


def test_unlisted_talent_gets_no_reply_to():
    """Part 3: 'If a talent or inbox is not listed here, leave Reply-To blank/default.'"""
    class Token:
        email = "talent-mgmt@taboost.me"
    assert reply_to_for_talent("Katrina2", token_row=Token()) is None
    assert reply_to_for_talent(None, token_row=Token()) is None


def test_every_alias_map_talent_is_either_routed_or_knowingly_blank():
    """A shared-inbox talent in no Part 3 group gets no Reply-To. That is correct
    for the five ex-creator-mgmt talents and a silent bug for anyone else — no
    error, no log line, and nobody finds out until a deal is lost. New aliases
    must be added to Part 3 or to NO_REPLY_TO_BY_DESIGN with a reason."""
    alias_map = (get_settings().app_config.get("single_inbox") or {}).get("alias_map") or {}
    listed = set().union(*_sop_part3_groups().values()) | NO_REPLY_TO_BY_DESIGN
    missing = sorted(a for a in alias_map if a.lower() not in listed)
    assert not missing, f"aliases neither in sop.md Part 3 nor knowingly blank: {missing}"

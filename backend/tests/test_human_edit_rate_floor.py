"""Human-edited drafts skip the verbatim SOP gate by design (an edit IS the
approval signal) — but that left the one thing the gate actually exists to catch,
a rate typo, with NO check at all on the edit path. This is narrower than
verbatim: it only rejects a dollar figure below the talent's floor.
"""
from unittest.mock import patch

from backend.models.db import Draft, DraftStatus
from backend.services.sop_parser import TalentProfile
from backend.services.validation import run_pre_send_checks

PROFILE = TalentProfile(
    key="Allee", full_name="Allee", minimum_rate_usd=500, manager="", paused=False,
    manager_email="", gmail_connection_name="", rate_unit="per video",
    auto_send=True, personal_emails=[], has_approved_response=True,
)


def _draft(text, edited=True):
    return Draft(
        talent_key="Allee", gmail_message_id="m1", status=DraftStatus.pending,
        draft_text=text, human_edited=edited,
    )


def test_rejects_a_below_floor_rate_on_a_human_edit(db_session):
    draft = _draft("Sure, we can do this one for $150 as a favor. " * 3)
    with patch("backend.services.validation.get_settings") as gs:
        gs.return_value.talent_profiles = {"Allee": PROFILE}
        ok, err = run_pre_send_checks(draft, db_session)
    assert ok is False
    assert "150" in err and "500" in err


def test_accepts_a_human_edit_at_or_above_the_floor(db_session):
    draft = _draft("Happy to confirm the $500 rate we discussed for this collab. " * 3)
    with patch("backend.services.validation.get_settings") as gs:
        gs.return_value.talent_profiles = {"Allee": PROFILE}
        ok, err = run_pre_send_checks(draft, db_session)
    assert ok is True, err


def test_a_draft_with_no_dollar_figure_is_not_flagged(db_session):
    draft = _draft("Thanks so much for reaching out, let me check her calendar. " * 3)
    with patch("backend.services.validation.get_settings") as gs:
        gs.return_value.talent_profiles = {"Allee": PROFILE}
        ok, err = run_pre_send_checks(draft, db_session)
    assert ok is True, err


def test_ai_generated_drafts_are_unaffected_by_the_floor_check(db_session):
    """Only the human-edit path gets this check; AI drafts still go through full
    verbatim enforcement exactly as before."""
    draft = _draft("Some non-verbatim AI text mentioning $100. " * 3, edited=False)
    with patch("backend.services.validation.get_settings") as gs, \
         patch("backend.services.reply.enforce_verbatim_response", return_value=None) as verbatim:
        gs.return_value.talent_profiles = {"Allee": PROFILE}
        run_pre_send_checks(draft, db_session)
    assert verbatim.called

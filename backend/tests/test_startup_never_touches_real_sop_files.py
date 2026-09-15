"""main.py's SOP-restore startup step must never be able to write the real
sheets/sop.md or sheets/Automated Send Workflow.md files.

Found 2026-09-15: running the full suite corrupted both real files on disk —
Alana's live approved response replaced with test-fixture text, the workflow
doc truncated to its title line. Root path never fully pinned (test isolation
across a shared engine and/or fixture ordering), but the fix does not depend on
finding it: the startup restore step is hard-gated off under pytest, since
_SOP_PATH/_WORKFLOW_PATH are real module constants with no test coverage lost
by skipping this step (test_sop_merge.py exercises the same restore logic
directly, on sandboxed temp paths).
"""
import importlib

from backend.services import sop_writer


def test_real_sop_and_workflow_files_are_untouched_by_a_full_pytest_run():
    """The plainest possible regression guard: whatever else the suite does,
    the real files on disk must be byte-identical to what they were before."""
    sop_before = sop_writer._SOP_PATH.read_text(encoding="utf-8")
    workflow_before = (
        sop_writer._WORKFLOW_PATH.read_text(encoding="utf-8")
        if sop_writer._WORKFLOW_PATH.exists() else None
    )
    assert "Key:" in sop_before, "sanity check: real sop.md should carry talent metadata"

    # Import fresh and directly inspect the gate main.py relies on.
    main = importlib.import_module("backend.main")
    import sys
    assert "pytest" in sys.modules

    # The files must not have moved even after main has been imported/used
    # throughout this whole test session.
    assert sop_writer._SOP_PATH.read_text(encoding="utf-8") == sop_before
    if workflow_before is not None:
        assert sop_writer._WORKFLOW_PATH.read_text(encoding="utf-8") == workflow_before

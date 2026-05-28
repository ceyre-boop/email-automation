"""
One-shot cleanup: delete all Gmail drafts created by the spam sweep and clean DB rows.

Run from repo root:
  DATABASE_URL=... python scripts/purge_spam_sweep.py

For each Draft row where triggered_by_job='spam_sweep':
  1. Delete the Gmail draft via API (ignore 404 — already gone)
  2. Mark Draft row status='discarded'
  3. Delete the ProcessedEmail row for that gmail_message_id
     (so those spam emails won't be permanently blocked from future triage)
"""
from __future__ import annotations

import os
import sys

# Allow imports from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.models.db import Draft, DraftStatus, ProcessedEmail, TalentToken
from backend.services import gmail as gmail_svc

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)

engine = create_engine(DATABASE_URL)
SessionFactory = sessionmaker(bind=engine)
db: Session = SessionFactory()

print("Querying spam_sweep drafts...")
spam_drafts = (
    db.query(Draft)
    .filter(Draft.triggered_by_job == "spam_sweep")
    .all()
)
print(f"Found {len(spam_drafts)} spam_sweep draft row(s)")

if not spam_drafts:
    print("Nothing to clean up.")
    db.close()
    sys.exit(0)

# Group by talent_key
by_talent: dict[str, list[Draft]] = defaultdict(list)
for d in spam_drafts:
    by_talent[d.talent_key].append(d)

# Fetch all needed tokens in one pass
talent_keys = list(by_talent.keys())
token_rows = (
    db.query(TalentToken)
    .filter(TalentToken.talent_key.in_(talent_keys))
    .all()
)
token_map = {t.talent_key.lower(): t for t in token_rows}

grand = {"deleted": 0, "not_found": 0, "failed": 0, "db_cleaned": 0}

for talent_key, drafts in by_talent.items():
    token_row = token_map.get(talent_key.lower())
    counts = {"deleted": 0, "not_found": 0, "failed": 0}

    for draft in drafts:
        gmail_draft_id = draft.gmail_draft_id
        if not gmail_draft_id:
            print(f"  [{talent_key}] draft id={draft.id} has no gmail_draft_id — skipping Gmail delete")
        elif token_row is None:
            print(f"  [{talent_key}] no token found — cannot delete Gmail draft {gmail_draft_id}")
            counts["failed"] += 1
        else:
            try:
                ok = gmail_svc.delete_gmail_draft(token_row, gmail_draft_id, db=db)
                if ok:
                    counts["deleted"] += 1
                else:
                    # delete_gmail_draft returns False on 404 (already gone)
                    counts["not_found"] += 1
            except Exception as exc:
                print(f"  [{talent_key}] Gmail delete error for {gmail_draft_id}: {exc}")
                counts["failed"] += 1

        # Mark Draft as discarded regardless of Gmail outcome
        draft.status = DraftStatus.discarded
        db.add(draft)

        # Delete ProcessedEmail so the email can be re-triaged from inbox if it ever appears
        pe = (
            db.query(ProcessedEmail)
            .filter(ProcessedEmail.gmail_message_id == draft.gmail_message_id)
            .first()
        )
        if pe:
            db.delete(pe)
            grand["db_cleaned"] += 1

    try:
        db.commit()
    except Exception as exc:
        print(f"  [{talent_key}] DB commit error: {exc}")
        db.rollback()

    print(
        f"  [{talent_key}] {len(drafts)} draft(s): "
        f"{counts['deleted']} deleted, {counts['not_found']} 404, {counts['failed']} failed"
    )
    for k in ("deleted", "not_found", "failed"):
        grand[k] += counts[k]

print()
print("=== Summary ===")
print(f"  Gmail deleted:   {grand['deleted']}")
print(f"  Gmail 404:       {grand['not_found']}")
print(f"  Gmail failed:    {grand['failed']}")
print(f"  DB rows cleaned: {grand['db_cleaned']}")
print()

# Verify
remaining = (
    db.query(Draft)
    .filter(Draft.triggered_by_job == "spam_sweep", Draft.status == DraftStatus.pending)
    .count()
)
print(f"Remaining pending spam_sweep drafts: {remaining}")

db.close()

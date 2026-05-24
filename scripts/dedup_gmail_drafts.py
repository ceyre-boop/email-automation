"""
Deduplicate Gmail drafts across all connected talent inboxes.

For each talent, fetches all drafts from Gmail, groups by thread_id,
and deletes all but the newest draft per thread. Also syncs the DB
so gmail_draft_id on processed_emails points to the surviving draft.

Usage:
    cd backend && python ../scripts/dedup_gmail_drafts.py
    cd backend && python ../scripts/dedup_gmail_drafts.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
import os

# Allow running from repo root or backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict
from googleapiclient.errors import HttpError

from backend.models.db import SessionLocal, TalentToken, ProcessedEmail
from backend.services.gmail import _gmail_service


def list_all_drafts(service) -> list[dict]:
    """Fetch every draft stub (id + threadId) from Gmail, paginating."""
    drafts = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            result = service.users().drafts().list(**kwargs).execute()
        except HttpError as exc:
            print(f"    ERROR listing drafts: {exc}")
            break
        for stub in result.get("drafts", []):
            draft_id = stub["id"]
            try:
                full = service.users().drafts().get(
                    userId="me", id=draft_id, format="metadata",
                    metadataHeaders=["Subject"]
                ).execute()
                thread_id = full.get("message", {}).get("threadId", "")
                drafts.append({"draft_id": draft_id, "thread_id": thread_id})
            except HttpError as exc:
                print(f"    WARN could not fetch draft {draft_id}: {exc}")
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return drafts


def dedup_talent(token_row, dry_run: bool, db) -> dict:
    print(f"\n  [{token_row.talent_key}] fetching drafts from Gmail...")
    try:
        service = _gmail_service(token_row, db)
    except Exception as exc:
        print(f"    ERROR building service: {exc}")
        return {"talent": token_row.talent_key, "error": str(exc)}

    all_drafts = list_all_drafts(service)
    print(f"    {len(all_drafts)} total drafts found")

    # Group by thread — keep newest (last in list from Gmail, which is creation order)
    by_thread: dict[str, list[str]] = defaultdict(list)
    for d in all_drafts:
        if d["thread_id"]:
            by_thread[d["thread_id"]].append(d["draft_id"])

    duplicate_threads = {tid: ids for tid, ids in by_thread.items() if len(ids) > 1}
    total_excess = sum(len(ids) - 1 for ids in duplicate_threads.values())
    print(f"    {len(duplicate_threads)} threads with duplicates, {total_excess} excess drafts to delete")

    deleted = 0
    db_synced = 0

    for thread_id, draft_ids in duplicate_threads.items():
        # Keep last draft_id (most recent), delete the rest
        keep = draft_ids[-1]
        to_delete = draft_ids[:-1]

        if dry_run:
            print(f"    [DRY RUN] thread {thread_id}: keep {keep}, delete {to_delete}")
        else:
            for draft_id in to_delete:
                try:
                    service.users().drafts().delete(userId="me", id=draft_id).execute()
                    deleted += 1
                except HttpError as exc:
                    print(f"    WARN delete failed for {draft_id}: {exc}")

            # Update DB row to point to the surviving draft
            row = db.query(ProcessedEmail).filter(
                ProcessedEmail.talent_key == token_row.talent_key,
                ProcessedEmail.thread_id == thread_id,
                ProcessedEmail.status == "draft_saved",
            ).first()
            if row and row.gmail_draft_id != keep:
                row.gmail_draft_id = keep
                db.add(row)
                db_synced += 1

    if not dry_run:
        db.commit()

    return {
        "talent": token_row.talent_key,
        "total_drafts": len(all_drafts),
        "duplicate_threads": len(duplicate_threads),
        "deleted": deleted,
        "db_synced": db_synced,
    }


def main():
    parser = argparse.ArgumentParser(description="Deduplicate Gmail drafts for all talents")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--talent", help="Only process this talent key (e.g. Katrina)")
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN — no changes will be made ===")

    db = SessionLocal()
    try:
        query = db.query(TalentToken)
        if args.talent:
            query = query.filter(TalentToken.talent_key == args.talent)
        tokens = query.all()

        if not tokens:
            print("No talent tokens found.")
            return

        print(f"Processing {len(tokens)} talent(s)...")
        results = []
        for token_row in tokens:
            result = dedup_talent(token_row, dry_run=args.dry_run, db=db)
            results.append(result)

        print("\n=== Summary ===")
        total_deleted = 0
        for r in results:
            if "error" in r:
                print(f"  {r['talent']}: ERROR — {r['error']}")
            else:
                print(f"  {r['talent']}: {r['total_drafts']} drafts, "
                      f"{r['duplicate_threads']} dup threads, "
                      f"{r['deleted']} deleted, {r['db_synced']} DB rows synced")
                total_deleted += r.get("deleted", 0)
        print(f"\nTotal deleted: {total_deleted}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

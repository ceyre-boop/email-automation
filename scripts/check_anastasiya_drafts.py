#!/usr/bin/env python3
"""
Check which of Anastasiya's 43 pending DB drafts exist as real Gmail drafts.
Prints two lists: valid (keep as pending) and orphaned (mark as discarded).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
db = Session()

# Get Anastasiya's token row
row = db.execute(text(
    "SELECT access_token, refresh_token, token_expiry FROM talent_tokens "
    "WHERE LOWER(talent_key) = 'anastasiya' AND active = true LIMIT 1"
)).fetchone()

if not row:
    print("ERROR: No active token for anastasiya")
    sys.exit(1)

creds = Credentials(
    token=row.access_token,
    refresh_token=row.refresh_token,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
)

service = build("gmail", "v1", credentials=creds, cache_discovery=False)

# Fetch all drafts from Gmail (paginate)
gmail_draft_ids = set()
page_token = None
while True:
    params = {"userId": "me", "maxResults": 500}
    if page_token:
        params["pageToken"] = page_token
    resp = service.users().drafts().list(**params).execute()
    for d in resp.get("drafts", []):
        gmail_draft_ids.add(d["id"])
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

print(f"Gmail has {len(gmail_draft_ids)} total drafts for Anastasiya")

# Get all 43 pending DB drafts
db_drafts = db.execute(text(
    "SELECT id, gmail_draft_id, gmail_message_id FROM drafts "
    "WHERE LOWER(talent_key) = 'anastasiya' AND status = 'pending' ORDER BY id"
)).fetchall()

valid_ids = []
orphan_ids = []

for d in db_drafts:
    if d.gmail_draft_id and d.gmail_draft_id in gmail_draft_ids:
        valid_ids.append(d.id)
        print(f"  VALID  DB#{d.id}  gmail_draft_id={d.gmail_draft_id}")
    else:
        orphan_ids.append(d.id)
        print(f"  ORPHAN DB#{d.id}  gmail_draft_id={d.gmail_draft_id}")

print()
print(f"Valid (keep pending): {valid_ids}")
print(f"Orphaned (discard):   {orphan_ids}")
print()
print("SQL to discard orphans:")
if orphan_ids:
    ids_str = ", ".join(str(i) for i in orphan_ids)
    print(f"""UPDATE drafts
SET status = 'discarded',
    reviewed_at = NOW(),
    reviewed_by = 'manual-cleanup-2026-05-27'
WHERE id IN ({ids_str});""")

db.close()

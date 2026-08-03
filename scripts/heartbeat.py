import os
import sys
import requests

url = os.environ["APP_BASE_URL"].rstrip("/") + "/cron/poll-inboxes"
api_key = os.environ["API_KEY"]
try:
    r = requests.get(url, headers={"x-api-key": api_key}, timeout=30)
    print(r.status_code, r.text)
    sys.exit(0 if r.ok else 1)
except Exception as e:
    print(f"Heartbeat failed: {e}", file=sys.stderr)
    sys.exit(1)

import os
import sys
import requests

url = os.environ["APP_BASE_URL"].rstrip("/") + "/cron/poll-inboxes"
try:
    r = requests.get(url, timeout=30)
    print(r.status_code, r.text)
    sys.exit(0 if r.ok else 1)
except Exception as e:
    print(f"Heartbeat failed: {e}", file=sys.stderr)
    sys.exit(1)

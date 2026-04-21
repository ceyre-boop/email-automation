#!/usr/bin/env python3
"""
Render entry point. Runs before uvicorn to print diagnostics,
then hands off to uvicorn.run() in the same process.
"""
import os
import sys

os.environ.setdefault("PYTHONUNBUFFERED", "1")

port = int(os.environ.get("PORT", 8000))

print("=== STARTUP DIAGNOSTICS ===", flush=True)
print(f"Python {sys.version}", flush=True)
print(f"PORT={port}", flush=True)
print(f"DATABASE_URL set: {bool(os.environ.get('DATABASE_URL'))}", flush=True)
print(f"GOOGLE_CLIENT_ID set: {bool(os.environ.get('GOOGLE_CLIENT_ID'))}", flush=True)
print(f"OPENAI_API_KEY set: {bool(os.environ.get('OPENAI_API_KEY'))}", flush=True)
print("=== IMPORTING APP ===", flush=True)

try:
    import uvicorn
    print("uvicorn imported OK", flush=True)
    from backend.main import app
    print("backend.main imported OK", flush=True)
except Exception as exc:
    import traceback
    print(f"FATAL IMPORT ERROR: {exc}", flush=True)
    traceback.print_exc()
    sys.exit(1)

print("=== STARTING SERVER ===", flush=True)
uvicorn.run(app, host="0.0.0.0", port=port)

#!/usr/bin/env bash
# Render start script — merges stderr into stdout so all Python output
# appears in Render deploy logs, and forces unbuffered output.
export PYTHONUNBUFFERED=1
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}" 2>&1

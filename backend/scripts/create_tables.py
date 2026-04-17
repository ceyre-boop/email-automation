"""
Standalone script to create all database tables.
Run once after setting up your Supabase project:

  cd email-automation
  python -m backend.scripts.create_tables
"""
from backend.models.db import create_tables

if __name__ == "__main__":
    print("Creating tables…")
    create_tables()
    print("Done.")

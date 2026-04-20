#!/usr/bin/env python3
"""
Generate a Google OAuth refresh token for the Sheets API.

Run this ONCE on your local machine to get a refresh token, then save
the printed token as the GitHub Secret GOOGLE_SHEETS_REFRESH_TOKEN.

Requirements
------------
  pip install google-auth-oauthlib

Usage
-----
  python scripts/generate_google_refresh_token.py

You will be prompted for your OAuth client credentials (or you can set them
as environment variables GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET).

⚠️  Your OAuth client MUST be of type "Desktop app" in Google Cloud Console.
     Web Application clients will NOT work for this script.
"""
from __future__ import annotations

import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit(
        "Missing dependency. Run:  pip install google-auth-oauthlib\n"
        "Then re-run this script."
    )

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def main() -> None:
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or input(
        "Enter your OAuth Client ID: "
    ).strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or input(
        "Enter your OAuth Client Secret: "
    ).strip()

    if not client_id or not client_secret:
        sys.exit("Client ID and Client Secret are required.")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)

    # Opens a browser window; after consent the token is exchanged automatically.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n" + "=" * 60)
    print("SUCCESS — copy the refresh token below into GitHub Secrets")
    print("  Secret name:  GOOGLE_SHEETS_REFRESH_TOKEN")
    print("=" * 60)
    print(creds.refresh_token)
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()

"""OAuth connection lifecycle for the owner's own YouTube channel, isolated
from the API-calling code in youtube.py so that module's functions can be
tested against a fake client without ever touching real OAuth machinery.
See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("playalongvideoproduction")

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
TOKEN_FILE = CREDENTIALS_DIR / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def connect(client_secrets_path: Path, token_path: Path = TOKEN_FILE):
    """Opens the owner's browser for one-time OAuth consent, then saves the
    resulting credentials (including refresh token) so future runs never
    need to ask again. Manually/visually verified (real browser interaction)
    -- see the spec's Testing section."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def load_credentials(token_path: Path = TOKEN_FILE):
    """None means "not connected" (never connected, or the stored token is
    unusable) -- callers always treat None this way, never raise."""
    if not token_path.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        log.warning("Could not read stored YouTube credentials: %s", exc)
        return None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not refresh YouTube credentials: %s", exc)
            return None
    return credentials


def get_channel_title(credentials) -> str:
    from googleapiclient.discovery import build

    youtube_client = build("youtube", "v3", credentials=credentials)
    response = youtube_client.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    return items[0]["snippet"]["title"] if items else "(unknown channel)"

"""OAuth connection lifecycle for the owner's own YouTube channel, isolated
from the API-calling code in youtube.py so that module's functions can be
tested against a fake client without ever touching real OAuth machinery.
See docs/superpowers/specs/2026-09-10-youtube-upload-design.md."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .youtube_state import atomic_write_text

log = logging.getLogger("playalongvideoproduction")

CREDENTIALS_DIR = Path.home() / ".playalongvideoproduction"
TOKEN_FILE = CREDENTIALS_DIR / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# The token file holds the channel's refresh token (full youtube.force-ssl control), so it is written owner-only (0600,
# in a 0700 folder) and atomically; and only one thread at a time may read-refresh-rewrite it -- the tick, upload and
# Approve workers all call load_credentials(), and after the hourly expiry two of them refreshing and rewriting the file
# at once could leave it unparseable ("not connected" until the owner re-consents). Issue #7 review, F141.
_TOKEN_LOCK = threading.RLock()


def _write_token(token_path: Path, text: str) -> None:
    token_path = Path(token_path)
    token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(token_path.parent, 0o700)
        except OSError as exc:
            log.warning("Could not restrict %s to owner-only: %s", token_path.parent, exc)
    atomic_write_text(token_path, text, mode=0o600)


def connect(client_secrets_path: Path, token_path: Path = TOKEN_FILE):
    """Opens the owner's browser for one-time OAuth consent, then saves the
    resulting credentials (including refresh token) so future runs never
    need to ask again. Manually/visually verified (real browser interaction)
    -- see the spec's Testing section."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    credentials = flow.run_local_server(port=0)
    with _TOKEN_LOCK:
        _write_token(token_path, credentials.to_json())
    return credentials


def load_credentials(token_path: Path = TOKEN_FILE):
    """None means "not connected" (never connected, or the stored token is
    unusable) -- callers always treat None this way, never raise. Serialized
    (a second caller waits, then reads the token the first one just
    refreshed instead of refreshing again)."""
    with _TOKEN_LOCK:
        return _load_credentials_locked(Path(token_path))


def _load_credentials_locked(token_path: Path):
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
            _write_token(token_path, credentials.to_json())
        except Exception as exc:
            log.warning("Could not refresh YouTube credentials: %s", exc)
            return None
    if credentials is None or not credentials.valid:
        # Expired with no refresh token to renew it (a token file from a
        # consent flow that never granted one) -- the file exists, but no
        # real call can succeed with it. Report "not connected" so the
        # owner reconnects, instead of handing back credentials that make
        # every upload/comment action fail with an auth error later.
        log.warning("Stored YouTube credentials are expired and can't be refreshed; reconnect")
        return None
    return credentials


def get_channel_title(credentials) -> str:
    from googleapiclient.discovery import build

    youtube_client = build("youtube", "v3", credentials=credentials)
    response = youtube_client.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    return items[0]["snippet"]["title"] if items else "(unknown channel)"

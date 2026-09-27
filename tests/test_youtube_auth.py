from lyricvideo.youtube_auth import load_credentials


def test_load_credentials_returns_none_when_no_token_file(tmp_path):
    assert load_credentials(tmp_path / "no_such_token.json") is None


def test_load_credentials_returns_none_on_corrupt_token_file(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("not valid json", encoding="utf-8")

    assert load_credentials(token_path) is None


def test_load_credentials_returns_none_for_an_expired_token_with_no_refresh_token(tmp_path):
    """The file exists but can't make a real call -- that's "not connected",
    not credentials to hand back and watch every YouTube action fail with."""
    import json

    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "stale-access-token",
        "refresh_token": None,
        "client_id": "id",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"],
        "expiry": "2000-01-01T00:00:00Z",
    }), encoding="utf-8")

    assert load_credentials(token_path) is None


def test_load_credentials_returns_a_still_valid_token_untouched(tmp_path):
    import json

    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "fresh-access-token",
        "refresh_token": None,
        "client_id": "id",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"],
        "expiry": "2999-01-01T00:00:00Z",
    }), encoding="utf-8")

    credentials = load_credentials(token_path)

    assert credentials is not None
    assert credentials.token == "fresh-access-token"


# --- the token is refreshed by one thread at a time and written owner-only (issue #7 review, F141) ----------------------

def _expired_token_file(tmp_path):
    import json

    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "stale", "refresh_token": "refresh-me", "client_id": "id", "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"], "expiry": "2000-01-01T00:00:00Z",
    }), encoding="utf-8")
    return token_path


def _fake_refresh(monkeypatch, counter):
    """Credentials.refresh without a network call: sleeps a moment (widening the race) and hands out a new token."""
    import datetime as _dt
    import time as _time

    from google.oauth2.credentials import Credentials

    def refresh(self, request):
        counter.append(1)
        _time.sleep(0.05)
        self.token = f"fresh-{len(counter)}-" + "x" * (len(counter) * 7)     # a different length each time
        self.expiry = _dt.datetime.utcnow() + _dt.timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", refresh)


def test_concurrent_loads_refresh_the_token_once_and_leave_a_readable_file(tmp_path, monkeypatch):
    import json
    import threading

    token_path = _expired_token_file(tmp_path)
    refreshes = []
    _fake_refresh(monkeypatch, refreshes)
    results = []

    threads = [threading.Thread(target=lambda: results.append(load_credentials(token_path))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(refreshes) == 1                        # the others waited and read the token it had just saved
    assert all(c is not None for c in results)
    assert json.loads(token_path.read_text(encoding="utf-8"))["token"].startswith("fresh-1-")
    assert [p.name for p in tmp_path.iterdir()] == ["token.json"]       # no temp file left behind


def test_a_refreshed_token_file_is_owner_only(tmp_path, monkeypatch):
    import os
    import stat

    import pytest

    if os.name != "posix":
        pytest.skip("file permission bits are a POSIX concept")
    token_path = _expired_token_file(tmp_path)
    _fake_refresh(monkeypatch, [])

    assert load_credentials(token_path) is not None
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700

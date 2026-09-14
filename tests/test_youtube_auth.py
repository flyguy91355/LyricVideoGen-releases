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

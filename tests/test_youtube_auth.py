from lyricvideo.youtube_auth import load_credentials


def test_load_credentials_returns_none_when_no_token_file(tmp_path):
    assert load_credentials(tmp_path / "no_such_token.json") is None


def test_load_credentials_returns_none_on_corrupt_token_file(tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("not valid json", encoding="utf-8")

    assert load_credentials(token_path) is None

from pathlib import Path

import httpx
import pytest

from lyricvideo.imagery import (
    build_image_prompt,
    generate_line_image,
    get_or_generate_image,
    is_fallback_image,
    substitute_fallback_images,
    summarize_song_gist,
    ImageGenError,
)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text="a moody forest at dusk"):
        self.messages = _FakeMessages(text)


def test_get_or_generate_image_reuses_backup_directory_instead_of_regenerating(tmp_path, monkeypatch):
    from lyricvideo.models import line_hash

    calls = {"n": 0}
    monkeypatch.setattr(
        "lyricvideo.imagery.generate_line_image",
        lambda *a, **k: calls.update(n=calls["n"] + 1) or Path("should-not-be-called"),
    )

    backup_dir = tmp_path / "images_backup_old"
    backup_dir.mkdir()
    key = line_hash("same line")
    (backup_dir / f"{key}.png").write_bytes(b"previously-paid-for-bytes")

    cache_dir = tmp_path / "images"
    path = get_or_generate_image(
        _FakeAnthropicClient(), "fake-token", "full lyrics", "same line", cache_dir,
        extra_cache_dirs=[backup_dir],
    )

    assert path.read_bytes() == b"previously-paid-for-bytes"
    assert calls["n"] == 0


def test_build_image_prompt_extracts_and_strips_text():
    client = _FakeAnthropicClient(text="  a lonely lighthouse in a storm  ")
    prompt = build_image_prompt(client, "a song about loss at sea", "the current line")
    assert prompt == "a lonely lighthouse in a storm"


def test_summarize_song_gist_extracts_and_strips_text():
    client = _FakeAnthropicClient(text="  a wistful song about missing an old friend  ")
    gist = summarize_song_gist(client, "full lyrics here")
    assert gist == "a wistful song about missing an old friend"


def test_get_or_generate_image_uses_cache(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_generate(replicate_token, prompt, out_path, model="black-forest-labs/flux-schnell"):
        calls["n"] += 1
        out_path.write_bytes(b"fake-png-bytes")
        return out_path

    monkeypatch.setattr("lyricvideo.imagery.generate_line_image", fake_generate)

    anthropic_client = _FakeAnthropicClient()

    path1 = get_or_generate_image(anthropic_client, "fake-token", "full lyrics", "same line", tmp_path)
    path2 = get_or_generate_image(anthropic_client, "fake-token", "full lyrics", "same line", tmp_path)

    assert path1 == path2
    assert calls["n"] == 1


def test_get_or_generate_image_falls_back_on_failure(tmp_path, capsys):
    class _FailingMessages:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    class _FailingAnthropicClient:
        def __init__(self):
            self.messages = _FailingMessages()

    path = get_or_generate_image(_FailingAnthropicClient(), "fake-token", "full lyrics", "a broken line", tmp_path)

    assert path.exists()
    from PIL import Image
    img = Image.open(path)
    assert img.size == (1920, 1080)

    # a silent fallback with no error visibility was the actual bug that hid the
    # real "Replicate: insufficient credit" failure -- must never regress to that
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "boom" in captured.err
    assert "falling back to a plain-color background" in captured.err


def test_get_or_generate_image_retries_three_times_before_falling_back(tmp_path, capsys):
    class _FailingMessages:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    class _FailingAnthropicClient:
        def __init__(self):
            self.messages = _FailingMessages()

    get_or_generate_image(_FailingAnthropicClient(), "fake-token", "full lyrics", "a broken line", tmp_path)

    captured = capsys.readouterr()
    assert "attempt 1/3" in captured.err
    assert "attempt 2/3" in captured.err
    assert "attempt 3/3" in captured.err


def test_is_fallback_image_detects_the_solid_color_placeholder(tmp_path):
    from PIL import Image

    fallback_path = tmp_path / "fallback.png"
    Image.new("RGB", (1920, 1080), (30, 30, 40)).save(fallback_path)

    assert is_fallback_image(fallback_path) is True


def test_is_fallback_image_false_for_a_real_varied_image(tmp_path):
    from PIL import Image

    real_path = tmp_path / "real.png"
    img = Image.new("RGB", (1920, 1080), (30, 30, 40))
    img.putpixel((0, 0), (200, 100, 50))  # one differing pixel -- not uniform
    img.save(real_path)

    assert is_fallback_image(real_path) is False


def test_is_fallback_image_false_for_solid_color_that_isnt_the_fallback_color(tmp_path):
    from PIL import Image

    path = tmp_path / "solid_but_different.png"
    Image.new("RGB", (1920, 1080), (10, 200, 10)).save(path)

    assert is_fallback_image(path) is False


def _make_image(path: Path, color: tuple[int, int, int]) -> Path:
    from PIL import Image

    Image.new("RGB", (1920, 1080), color).save(path)
    return path


def test_substitute_fallback_images_forward_fills_from_previous_real_image(tmp_path):
    real1 = _make_image(tmp_path / "1.png", (10, 20, 30))
    fallback = _make_image(tmp_path / "2.png", (30, 30, 40))
    real2 = _make_image(tmp_path / "3.png", (40, 50, 60))

    substitute_fallback_images([real1, fallback, real2])

    assert not is_fallback_image(fallback)
    assert fallback.read_bytes() == real1.read_bytes()


def test_substitute_fallback_images_backward_fills_a_leading_fallback(tmp_path):
    fallback = _make_image(tmp_path / "1.png", (30, 30, 40))
    real = _make_image(tmp_path / "2.png", (10, 20, 30))

    substitute_fallback_images([fallback, real])

    assert not is_fallback_image(fallback)
    assert fallback.read_bytes() == real.read_bytes()


def test_substitute_fallback_images_leaves_fallback_when_every_image_failed(tmp_path):
    fallback1 = _make_image(tmp_path / "1.png", (30, 30, 40))
    fallback2 = _make_image(tmp_path / "2.png", (30, 30, 40))

    substitute_fallback_images([fallback1, fallback2])

    assert is_fallback_image(fallback1)
    assert is_fallback_image(fallback2)


def test_substitute_fallback_images_ignores_missing_paths(tmp_path):
    missing = tmp_path / "does-not-exist.png"
    real = _make_image(tmp_path / "2.png", (10, 20, 30))

    substitute_fallback_images([missing, real])  # must not raise

    assert not missing.exists()


class _FakeHttpResponse:
    def __init__(self, json_data=None, status_code=200, content=b""):
        self._json = json_data
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeHttpClient:
    """Simulates: create -> processing, one transient 503 poll, then succeeded, then image download."""

    def __init__(self):
        self.poll_calls = 0
        self.post_kwargs = None

    def post(self, url, **kwargs):
        self.post_kwargs = kwargs
        return _FakeHttpResponse(
            {
                "id": "pred123",
                "status": "starting",
                "output": None,
                "urls": {"get": "https://api.replicate.com/v1/predictions/pred123"},
            }
        )

    def get(self, url, **kwargs):
        if "predictions/pred123" in url:
            self.poll_calls += 1
            if self.poll_calls == 1:
                return _FakeHttpResponse(status_code=503)  # transient error
            return _FakeHttpResponse({"status": "succeeded", "output": ["https://example.com/img.png"]})
        return _FakeHttpResponse(content=b"real-image-bytes")


def test_generate_line_image_polls_through_transient_error_then_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr("lyricvideo.imagery.REPLICATE_POLL_INTERVAL_SECONDS", 0.0)
    http_client = _FakeHttpClient()
    out_path = tmp_path / "img.png"

    result = generate_line_image("fake-token", "a prompt", out_path, http_client=http_client)

    assert result == out_path
    assert out_path.read_bytes() == b"real-image-bytes"
    assert http_client.poll_calls == 2  # one 503, then one success
    assert http_client.post_kwargs["json"]["input"]["aspect_ratio"] == "16:9"


def test_generate_line_image_raises_on_failed_status():
    class _FailedHttpClient:
        def post(self, url, **kwargs):
            return _FakeHttpResponse({"id": "p1", "status": "failed", "output": None, "urls": {"get": "https://x/p1"}})

        def get(self, url, **kwargs):
            return _FakeHttpResponse({"status": "failed", "output": None})

    with pytest.raises(ImageGenError):
        generate_line_image("fake-token", "a prompt", Path("unused.png"), http_client=_FailedHttpClient())


def test_generate_line_image_raises_on_timeout(monkeypatch):
    monkeypatch.setattr("lyricvideo.imagery.REPLICATE_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr("lyricvideo.imagery.REPLICATE_POLL_TIMEOUT_SECONDS", 0.0)

    class _NeverDoneHttpClient:
        def post(self, url, **kwargs):
            return _FakeHttpResponse({"id": "p1", "status": "starting", "output": None, "urls": {"get": "https://x/p1"}})

        def get(self, url, **kwargs):
            return _FakeHttpResponse({"status": "processing", "output": None})

    with pytest.raises(ImageGenError):
        generate_line_image("fake-token", "a prompt", Path("unused.png"), http_client=_NeverDoneHttpClient())

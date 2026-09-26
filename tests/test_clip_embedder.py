import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import lyricvideo.clip_embedder as clip_embedder
from lyricvideo.clip_embedder import ClipEmbedder, EmbedderUnavailable


def test_importing_the_module_does_not_import_open_clip_or_torch():
    # a fresh interpreter: the lazy import is what keeps the GUI's launch as fast as before
    code = (
        "import sys, lyricvideo.clip_embedder; "
        "sys.exit(0 if 'open_clip' not in sys.modules and 'torch' not in sys.modules else 1)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent)
    assert result.returncode == 0


def test_unavailable_when_open_clip_is_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "open_clip", None)  # makes `import open_clip` raise ImportError

    with pytest.raises(EmbedderUnavailable, match="open_clip"):
        ClipEmbedder().check_available()


def test_unavailable_when_the_weights_are_not_downloaded(monkeypatch):
    monkeypatch.setattr(clip_embedder, "_require_open_clip", lambda: None)

    def _not_cached(*args, **kwargs):
        assert kwargs["local_files_only"] is True  # a pipeline run must never start a download
        raise OSError("not in the local cache")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", _not_cached)

    with pytest.raises(EmbedderUnavailable, match="not downloaded"):
        ClipEmbedder(allow_download=False).check_available()


def test_allow_download_lets_the_weights_be_fetched(monkeypatch, tmp_path):
    monkeypatch.setattr(clip_embedder, "_require_open_clip", lambda: None)
    seen = {}

    def _fetch(repo_id, filename, local_files_only):
        seen.update(repo_id=repo_id, filename=filename, local_files_only=local_files_only)
        return str(tmp_path / filename)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", _fetch)

    path = ClipEmbedder(allow_download=True).check_available()

    assert path == tmp_path / clip_embedder.WEIGHTS_FILENAME
    assert seen == {
        "repo_id": clip_embedder.HF_REPO_ID, "filename": clip_embedder.WEIGHTS_FILENAME, "local_files_only": False,
    }


# --- opt-in: the real model, only when open_clip is installed AND the weights are already cached -------------

@pytest.fixture(scope="module")
def real_embedder():
    pytest.importorskip("open_clip")
    embedder = ClipEmbedder(allow_download=False)
    try:
        embedder.check_available()
    except EmbedderUnavailable as e:
        pytest.skip(f"CLIP weights not cached: {e}")
    yield embedder
    embedder.close()


def _solid(tmp_path: Path, name: str, color) -> Path:
    path = tmp_path / name
    Image.new("RGB", (320, 180), color).save(path)
    return path


def test_real_model_returns_normalized_512_vectors(real_embedder, tmp_path):
    images = real_embedder.embed_images(
        [_solid(tmp_path, "r.png", (255, 0, 0)), _solid(tmp_path, "b.png", (0, 0, 255))]
    )
    text = real_embedder.embed_text(["a red square", "a blue square"])

    assert images.shape == (2, 512) and text.shape == (2, 512)
    assert np.allclose(np.linalg.norm(images, axis=1), 1.0, atol=1e-4)
    assert np.allclose(np.linalg.norm(text, axis=1), 1.0, atol=1e-4)


def test_real_model_matches_a_colour_word_to_the_right_picture(real_embedder, tmp_path):
    red = real_embedder.embed_images([_solid(tmp_path, "r.png", (255, 0, 0))])[0]
    blue = real_embedder.embed_images([_solid(tmp_path, "b.png", (0, 0, 255))])[0]
    text = real_embedder.embed_text(["a plain red background"])[0]

    assert float(text @ red) > float(text @ blue)


def test_real_model_embeds_a_prompt_far_longer_than_clips_77_token_window(real_embedder):
    vector = real_embedder.embed_text([" ".join(["a lonely lighthouse in a storm at dusk"] * 60)])

    assert vector.shape == (1, 512) and np.isfinite(vector).all()

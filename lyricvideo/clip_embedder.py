"""Local CLIP image/text embeddings for the shared image library (spec 2026-09-25).

`open_clip` and `torch` are imported lazily, only when a model is actually needed, so nothing else
in the app (the GUI's launch, every other stage) pays for them. A pipeline run passes
allow_download=False and therefore NEVER starts the ~605 MB weights download -- if the weights are not
already on disk it raises EmbedderUnavailable and the images stage buys as usual. Only
scripts/import_image_library.py (started by the owner) constructs ClipEmbedder(allow_download=True).

Weights: the HF repo laion/CLIP-ViT-B-32-laion2B-s34B-b79K holds four ~605 MB copies of them; only
open_clip_model.safetensors is fetched. CPU only (the owner's GT 1030 has 2 GB and CPU is plenty)."""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from .image_library import EMBEDDING_DIM

MODEL_NAME = "ViT-B-32"
HF_REPO_ID = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"
WEIGHTS_FILENAME = "open_clip_model.safetensors"
_IMAGE_BATCH = 32


class EmbedderUnavailable(Exception):
    """The CLIP model can't be used right now (package missing, or weights not downloaded)."""


class Embedder(Protocol):
    def embed_images(self, paths: Sequence[Path]) -> np.ndarray: ...   # (n, 512), L2-normalized rows
    def embed_text(self, texts: Sequence[str]) -> np.ndarray: ...      # (n, 512), L2-normalized rows


def _require_open_clip() -> None:
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
    except ImportError as e:
        raise EmbedderUnavailable(
            f"open_clip is not installed ({e}) -- run: .venv/bin/python -m pip install open_clip_torch"
        ) from e


def _locate_weights(allow_download: bool) -> Path:
    from huggingface_hub import hf_hub_download

    try:
        return Path(hf_hub_download(HF_REPO_ID, WEIGHTS_FILENAME, local_files_only=not allow_download))
    except Exception as e:
        raise EmbedderUnavailable(
            f"CLIP weights are not downloaded ({type(e).__name__}) -- run scripts/import_image_library.py once"
        ) from e


class ClipEmbedder:
    def __init__(self, allow_download: bool = False):
        self.allow_download = allow_download
        self._weights_path: Path | None = None
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._torch = None

    def check_available(self) -> Path:
        """Cheap: confirms open_clip imports and the weights file is on disk (downloading it only when
        allow_download is set). Does not load the model."""
        _require_open_clip()
        self._weights_path = _locate_weights(self.allow_download)
        return self._weights_path

    def _load(self) -> None:
        if self._model is not None:
            return
        weights = self._weights_path or self.check_available()
        import open_clip
        import torch

        model, _, preprocess = open_clip.create_model_and_transforms(MODEL_NAME, pretrained=str(weights), device="cpu")
        model.eval()
        self._model, self._preprocess = model, preprocess
        self._tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        self._torch = torch

    def _normalized(self, features) -> np.ndarray:
        features = features / features.norm(dim=-1, keepdim=True)
        return features.float().cpu().numpy()

    def embed_images(self, paths: Sequence[Path]) -> np.ndarray:
        from PIL import Image

        self._load()
        chunks = []
        for start in range(0, len(paths), _IMAGE_BATCH):
            tensors = []
            for path in paths[start:start + _IMAGE_BATCH]:
                with Image.open(path) as image:
                    tensors.append(self._preprocess(image.convert("RGB")))
            with self._torch.no_grad():
                chunks.append(self._normalized(self._model.encode_image(self._torch.stack(tensors))))
        return np.vstack(chunks) if chunks else np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

    def embed_text(self, texts: Sequence[str]) -> np.ndarray:
        self._load()
        tokens = self._tokenizer(list(texts))   # CLIP reads at most 77 tokens; longer prompts are truncated
        with self._torch.no_grad():
            return self._normalized(self._model.encode_text(tokens))

    def close(self) -> None:
        """Frees the model (~600 MB) -- called at the end of the images stage."""
        self._model = self._preprocess = self._tokenizer = self._torch = None
        gc.collect()

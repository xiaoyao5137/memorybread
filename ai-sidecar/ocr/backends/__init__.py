from __future__ import annotations

from .base   import OcrBackend, OcrBox, OcrOutput
from .paddle import PaddleBackend
from .vision_pyobjc import AppleVisionBackend

__all__ = ["OcrBackend", "OcrBox", "OcrOutput", "PaddleBackend", "AppleVisionBackend"]

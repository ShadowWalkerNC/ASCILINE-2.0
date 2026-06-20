"""
core/decoder.py
===============
VideoDecoder and AsciiMapper — the two core rendering primitives.

Moved from ascii_video_player2.py so both the web server and the
standalone terminal player can import from one canonical location.

Quality improvements applied vs. original:
  - INTER_AREA downscaling  : mathematically correct for shrinking;
                               preserves edges, eliminates aliasing.
  - Gamma-corrected brightness: maps linear pixel values through a
                               perceptual curve before the character
                               LUT so shadow/highlight gradients are
                               represented faithfully (not crushed).
"""

import sys
import numpy as np
import cv2
import os

os.system("")  # Enable ANSI codes on Windows (PowerShell / CMD)


# ─────────────────────────────────────────────
#  MODULE 1 — VideoDecoder
# ─────────────────────────────────────────────
class VideoDecoder:
    """
    Opens a video file (or direct URL) and yields (gray, bgr) pairs.

    Parameters
    ----------
    path       : local filesystem path or http(s) URL
    cols, rows : target grid dimensions
    skip_gray  : set True in pixel mode — skips grayscale conversion
    """

    def __init__(self, path: str, cols: int, rows: int, skip_gray: bool = False) -> None:
        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            raise FileNotFoundError(f"Could not open video file: {path!r}")

        self.fps         : float = self._cap.get(cv2.CAP_PROP_FPS) or 24.0
        self.frame_count : int   = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.vid_w       : int   = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.vid_h       : int   = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._size       : tuple = (cols, rows)
        self._skip_gray  : bool  = skip_gray

    def __iter__(self):
        return self

    def __next__(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Yields (gray[H,W] uint8, bgr[H,W,3] uint8).
        gray is None when skip_gray=True (pixel mode optimisation).

        INTER_AREA is used for downscaling: it computes the true area
        average of source pixels mapped to each destination pixel,
        which is the correct anti-aliasing filter for shrink operations
        and retains far more detail than bilinear at small grid sizes.
        """
        ok, frame = self._cap.read()
        if not ok:
            raise StopIteration

        # QUALITY FIX 1: INTER_AREA instead of INTER_LINEAR
        small = cv2.resize(frame, self._size, interpolation=cv2.INTER_AREA)

        if self._skip_gray:
            return None, small

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        # QUALITY FIX 2: gamma-corrected brightness
        # Human vision is logarithmic — sqrt approximates the sRGB transfer
        # function and spreads dark tones across more character levels,
        # recovering crushed shadow detail at no measurable speed cost.
        gray = (np.sqrt(gray.astype(np.float32) / 255.0) * 255.0).astype(np.uint8)

        return gray, small

    def release(self):
        self._cap.release()

    def grab(self) -> bool:
        """Advance one frame without decoding (used for FPS decimation)."""
        return self._cap.grab()

    def seek(self, target_sec: float) -> bool:
        """Seek to target_sec seconds."""
        if self._cap:
            return self._cap.set(cv2.CAP_PROP_POS_MSEC, target_sec * 1000)
        return False

    def __del__(self):
        self.release()


# ─────────────────────────────────────────────
#  MODULE 2 — AsciiMapper
# ─────────────────────────────────────────────
class AsciiMapper:
    """
    Converts (gray, BGR) matrix pair into a coloured ANSI string.

    Technique
    ---------
    1. Gray value → character index via a 93-level intensity LUT.
    2. BGR → RGB, optional bit-quantisation for RLE efficiency.
    3. RLE: escape code written only on colour change per row.
       Typical frame: 40–60 % smaller string vs. per-pixel codes.
    """

    DEFAULT_PALETTE = list(
        " `.-':_,^=;><+!rc*/z?sLTv)J7(|Fi{C}fI31tlu[neoZ5Yxjya]2ESwqkP6h9d4VpOGbUAKXHm8RD#$Bg0MNWQ%&@"
    )
    _RESET = "\033[0m"

    def __init__(self, palette: list[str] | None = None, quantize_bits: int = 0) -> None:
        p = palette or self.DEFAULT_PALETTE
        self._n   = len(p)
        self._lut = np.array(p, dtype="U1")
        self._qb  = quantize_bits

    def convert(self, gray: np.ndarray, bgr: np.ndarray) -> str:
        H, W = gray.shape
        indices = np.floor_divide(gray, max(1, 256 // self._n))
        np.clip(indices, 0, self._n - 1, out=indices)
        char_matrix = self._lut[indices]

        rgb = bgr[:, :, ::-1]
        if self._qb > 0:
            qb = self._qb
            rgb = (rgb >> qb) << qb

        lines = []
        prev_r = prev_g = prev_b = -1
        for row_idx in range(H):
            row_chars  = char_matrix[row_idx]
            row_colors = rgb[row_idx]
            buf = []
            for col_idx in range(W):
                r = int(row_colors[col_idx, 0])
                g = int(row_colors[col_idx, 1])
                b = int(row_colors[col_idx, 2])
                if r != prev_r or g != prev_g or b != prev_b:
                    buf.append(f"\033[38;2;{r};{g};{b}m")
                    prev_r, prev_g, prev_b = r, g, b
                buf.append(row_chars[col_idx])
            lines.append("".join(buf))

        return self._RESET + "\n".join(lines) + self._RESET

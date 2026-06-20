"""
core/scrub.py
=============
Scrub-preview sprite builder.

Builds a JPEG contact sheet of thumbnail frames sampled across a video,
powering the hover-preview strip on the seek bar in the web UI.

Built once per video on first request; held in memory only (no disk I/O).
Disable entirely with --no-thumbnails.
"""

import math
import subprocess
import cv2


def build_scrub_sprite(
    video_path: str,
    max_count: int = 64,
    cell_w: int = 160,
) -> dict | None:
    """
    Build a tiled JPEG sprite for seek-bar hover previews.

    Returns a dict with keys:
        meta  : dict  — grid dimensions, timing metadata
        jpeg  : bytes — raw JPEG bytes of the sprite image
    or None if the video cannot be probed or ffmpeg is unavailable.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None

    fps      = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total    = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w0       = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h0       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    duration = (total / fps) if fps else 0
    if duration <= 0 or w0 <= 0 or h0 <= 0:
        return None

    cell_h   = max(1, round(cell_w * h0 / w0))
    n        = max(1, min(max_count, int(duration)))
    cols     = max(1, math.ceil(math.sqrt(n)))
    rows     = max(1, math.ceil(n / cols))
    interval = duration / n

    vf = f"fps={n}/{duration:.3f},scale={cell_w}:{cell_h},tile={cols}x{rows}"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", video_path,
             "-vf", vf, "-frames:v", "1", "-q:v", "4",
             "-f", "image2", "-c:v", "mjpeg", "-loglevel", "error", "pipe:1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0 or not proc.stdout:
        return None

    return {
        "meta": {
            "available": True,
            "count":     n,
            "gridCols":  cols,
            "gridRows":  rows,
            "cellW":     cell_w,
            "cellH":     cell_h,
            "interval":  interval,
            "duration":  duration,
        },
        "jpeg": proc.stdout,
    }

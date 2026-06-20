"""
core/queue_manager.py
=====================
Builds and manages the video playback queue.

Supports three source modes (in priority order):
  1. --playlist  JSON file  (per-video mode, vol, cols, pixel)
  2. --folder    directory  (filesystem order, shared global settings)
  3. positional  single video or URL

URL resolution (yt-dlp) lives here so both CLI and API enqueue share
the exact same resolution logic.
"""

import os
import json
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─── URL / path resolution ────────────────────────────────────────────────────

def resolve_video_path(video: str) -> str:
    """
    Resolve a local video path by checking multiple locations:
      1. As-is (absolute or relative to CWD)
      2. Inside project root (BASE_DIR)
      3. Inside BASE_DIR/videos/ subfolder
    """
    candidates = [
        video,
        os.path.join(BASE_DIR, video),
        os.path.join(BASE_DIR, "videos", os.path.basename(video)),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return video  # let playback surface the error


def resolve_video_source(video: str) -> str:
    """
    Resolve any video source to something cv2.VideoCapture can open:
      - Direct media URLs (.mp4/.webm/etc.) → returned as-is
      - Platform URLs (YouTube, Twitch …)   → resolved via yt-dlp
      - Local paths                          → resolve_video_path()
    """
    stripped = video.strip()
    if stripped.startswith(("http://", "https://")):
        DIRECT_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".m3u8")
        parsed_path = urlparse(stripped).path.lower()
        if any(parsed_path.endswith(ext) for ext in DIRECT_EXTS):
            return stripped
        try:
            import yt_dlp
            ydl_opts = {
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(stripped, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                url = info.get("url") or info.get("manifest_url")
                if url:
                    print(f"[yt-dlp] Resolved: {stripped[:60]}... → CDN stream")
                    return url
        except ImportError:
            print("[yt-dlp] Not installed. pip install yt-dlp")
        except Exception as e:
            print(f"[yt-dlp] Resolution failed: {e}")
        return stripped
    return resolve_video_path(stripped)


# ─── Queue builders ───────────────────────────────────────────────────────────

def load_playlist(playlist_path: str) -> list[dict]:
    """Load a JSON playlist and resolve all video sources."""
    with open(playlist_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    for item in items:
        item["video"] = resolve_video_source(item["video"])
    return items


def load_folder(folder_path: str, default_mode: int, default_vol: int) -> list[dict]:
    """
    Scan a folder for video files in filesystem order (not alphabetical).
    """
    supported = (".mp4", ".mkv", ".avi", ".mov", ".webm")
    entries = []
    with os.scandir(folder_path) as it:
        for entry in it:
            if entry.is_file() and entry.name.lower().endswith(supported):
                entries.append({
                    "video": entry.path,
                    "mode":  default_mode,
                    "vol":   default_vol,
                })
    return entries


def build_queue(args) -> list[dict]:
    """
    Build the playback queue from parsed CLI args.
    Priority: --playlist > --folder > single video argument.
    """
    if args.playlist:
        print(f"[PLAYLIST] Loading: {args.playlist}")
        items = load_playlist(args.playlist)
        for item in items:
            item.setdefault("mode",  args.mode)
            item.setdefault("vol",   args.vol)
            item.setdefault("pixel", args.pixel)
            is_pixel = item.get("pixel", False)
            default_cols = args.cols if args.cols is not None else (450 if is_pixel else 200)
            item.setdefault("cols", default_cols)
            item.setdefault("rows", args.rows)
        return items

    if args.folder:
        print(f"[FOLDER] Scanning: {args.folder}")
        items = load_folder(args.folder, args.mode, args.vol)
        default_cols = args.cols if args.cols is not None else (450 if args.pixel else 200)
        for item in items:
            item["pixel"] = args.pixel
            item["cols"]  = default_cols
            item["rows"]  = args.rows
        return items

    video_source = resolve_video_source(args.video)
    default_cols = args.cols if args.cols is not None else (450 if args.pixel else 200)
    return [{
        "video": video_source,
        "mode":  args.mode,
        "vol":   args.vol,
        "pixel": args.pixel,
        "cols":  default_cols,
        "rows":  args.rows,
    }]

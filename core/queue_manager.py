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

Direct mp4/webm/etc. URLs:
  On cloud/headless deployments, cv2.VideoCapture can stall or loop
  when streaming remote HTTP URLs directly. We always pre-download
  direct media URLs to /tmp so playback is stable and fast.
"""

import os
import json
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Extensions cv2+FFmpeg can stream directly over HTTP
DIRECT_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".m3u8", ".ts")

# Platform URL patterns that require yt-dlp
PLATFORM_HINTS = (
    "youtube.com", "youtu.be",
    "twitch.tv", "vimeo.com",
    "dailymotion.com", "tiktok.com",
    "instagram.com", "twitter.com", "x.com",
    "reddit.com", "streamable.com",
)


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


def _is_platform_url(url: str) -> bool:
    """Return True if the URL is a known platform that needs yt-dlp."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return any(hint in host for hint in PLATFORM_HINTS)


def _is_direct_media_url(url: str) -> bool:
    """Return True if the URL points directly to a media file."""
    parsed_path = urlparse(url).path.lower()
    return any(parsed_path.endswith(ext) for ext in DIRECT_EXTS)


def _download_to_tmp(url: str) -> str:
    """
    Download a remote URL to /tmp and return the local path.
    Shows a progress indicator. If download fails, returns original URL.
    """
    import urllib.request
    import tempfile
    import hashlib

    # Use a stable filename based on URL hash so re-deploys reuse cached files
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    ext = os.path.splitext(urlparse(url).path)[-1] or ".mp4"
    tmp_path = os.path.join("/tmp", f"asciline_{url_hash}{ext}")

    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1024:
        print(f"[cache] Reusing {tmp_path}")
        return tmp_path

    print(f"[download] Fetching {url[:80]} ...")
    try:
        def _progress(count, block, total):
            if total > 0 and count % 50 == 0:
                pct = min(100, int(count * block * 100 / total))
                print(f"[download] {pct}%", flush=True)

        urllib.request.urlretrieve(url, tmp_path, reporthook=_progress)
        size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        print(f"[download] Done → {tmp_path} ({size_mb:.1f} MB)")
        return tmp_path
    except Exception as e:
        print(f"[download] Failed: {e} — falling back to direct stream")
        return url


def resolve_video_source(video: str) -> str:
    """
    Resolve any video source to something cv2.VideoCapture can open:

      1. Direct media URL (.mp4/.webm/etc.):
         - Always pre-downloaded to /tmp for stable local playback.
         - Cached by URL hash so restarts reuse the same file.

      2. Platform URL (YouTube, Twitch, Vimeo …):
         - Resolved via yt-dlp to a CDN stream URL.
         - If yt-dlp fails (bot detection, no auth), logs the error.

      3. Local path:
         - Searched in CWD, project root, and videos/ subfolder.
    """
    stripped = video.strip()

    if stripped.startswith(("http://", "https://")):
        # ── Case 1: direct media URL → download to /tmp ──
        if _is_direct_media_url(stripped) and not _is_platform_url(stripped):
            return _download_to_tmp(stripped)

        # ── Case 2: platform URL → yt-dlp ──
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
                    print(f"[yt-dlp] Resolved: {stripped[:60]}")
                    return url
        except ImportError:
            print("[yt-dlp] Not installed. pip install yt-dlp")
        except Exception as e:
            print(f"[yt-dlp] Resolution failed: {e}")
            if _is_direct_media_url(stripped):
                return _download_to_tmp(stripped)

        return stripped  # return as-is, let VideoDecoder surface the error

    # ── Case 3: local path ──
    return resolve_video_path(stripped)


# ─── Queue builders ──────────────────────────────────────────────────────────────

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

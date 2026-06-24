"""
core/queue_manager.py — cloud-safe URL resolution.
All remote URLs are downloaded to /tmp BEFORE cv2 ever sees them.
If a download fails the entry is dropped (RuntimeError) so the
loop never retries a broken source forever.
"""

import os
import json
import hashlib
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECT_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".m3u8", ".ts")
PLATFORM_HINTS = (
    "youtube.com", "youtu.be", "twitch.tv", "vimeo.com",
    "dailymotion.com", "tiktok.com", "instagram.com",
    "twitter.com", "x.com", "reddit.com", "streamable.com",
)


def resolve_video_path(video):
    candidates = [
        video,
        os.path.join(BASE_DIR, video),
        os.path.join(BASE_DIR, "videos", os.path.basename(video)),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return video


def _is_platform_url(url):
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return any(h in host for h in PLATFORM_HINTS)


def _tmp_path(url):
    h = hashlib.md5(url.encode()).hexdigest()[:10]
    ext = os.path.splitext(urlparse(url).path)[-1] or ".mp4"
    return f"/tmp/asciline_{h}{ext}"


def download_to_tmp(url):
    """Download url → /tmp. Returns local path or raises RuntimeError."""
    import urllib.request
    dest = _tmp_path(url)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        print(f"[cache] {dest}", flush=True)
        return dest
    print(f"[download] {url[:80]} ...", flush=True)
    try:
        def _prog(n, bs, tot):
            if tot > 0 and n % 100 == 0:
                print(f"[download] {min(100, n*bs*100//tot)}%", flush=True)
        urllib.request.urlretrieve(url, dest, reporthook=_prog)
        mb = os.path.getsize(dest) / 1048576
        print(f"[download] done → {dest} ({mb:.1f} MB)", flush=True)
        return dest
    except Exception as e:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise RuntimeError(f"Download failed for {url!r}: {e}")


def resolve_video_source(video):
    """
    Returns a LOCAL path for cv2. Never returns a raw http(s) URL.
    Raises RuntimeError if remote download fails.
    """
    s = video.strip()
    if not s.startswith(("http://", "https://")):
        return resolve_video_path(s)

    if _is_platform_url(s):
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"format": "best[ext=mp4]/best", "quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(s, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                cdn = info.get("url") or info.get("manifest_url")
                if cdn:
                    s = cdn
        except ImportError:
            raise RuntimeError("yt-dlp not installed")
        except Exception as e:
            raise RuntimeError(f"yt-dlp failed: {e}")

    return download_to_tmp(s)


def load_playlist(path):
    with open(path, "r", encoding="utf-8") as f:
        items = json.load(f)
    out = []
    for item in items:
        try:
            item["video"] = resolve_video_source(item["video"])
            out.append(item)
        except RuntimeError as e:
            print(f"[playlist] skip: {e}")
    return out


def load_folder(folder, default_mode, default_vol):
    supported = (".mp4", ".mkv", ".avi", ".mov", ".webm")
    entries = []
    with os.scandir(folder) as it:
        for e in it:
            if e.is_file() and e.name.lower().endswith(supported):
                entries.append({"video": e.path, "mode": default_mode, "vol": default_vol})
    return entries


def build_queue(args):
    """Build queue. Blocks until all remote URLs are downloaded."""
    if args.playlist:
        print(f"[PLAYLIST] {args.playlist}")
        items = load_playlist(args.playlist)
        for item in items:
            item.setdefault("mode", args.mode)
            item.setdefault("vol", args.vol)
            item.setdefault("pixel", args.pixel)
            px = item.get("pixel", False)
            item.setdefault("cols", args.cols if args.cols else (450 if px else 200))
            item.setdefault("rows", args.rows)
        return items

    if args.folder:
        print(f"[FOLDER] {args.folder}")
        items = load_folder(args.folder, args.mode, args.vol)
        cols = args.cols if args.cols else (450 if args.pixel else 200)
        for item in items:
            item["pixel"] = args.pixel
            item["cols"] = cols
            item["rows"] = args.rows
        return items

    try:
        src = resolve_video_source(args.video)
    except RuntimeError as e:
        print(f"[ERROR] {e} — starting with empty queue")
        return []

    cols = args.cols if args.cols else (450 if args.pixel else 200)
    return [{"video": src, "mode": args.mode, "vol": args.vol,
             "pixel": args.pixel, "cols": cols, "rows": args.rows}]

"""
ytdl.py
=======
Thin wrapper around yt-dlp for downloading and caching videos.
Handles single videos, playlists, and channels.
"""

import os
import subprocess
import json
from urllib.parse import urlparse


def is_url(s: str) -> bool:
    """Return True if *s* looks like an HTTP(S) or other network URL."""
    try:
        result = urlparse(s)
        return result.scheme in ("http", "https", "ftp", "ftps", "rtmp", "rtsp")
    except ValueError:
        return False


def _ydl_base_cmd() -> list[str]:
    """Return the base yt-dlp invocation."""
    return ["yt-dlp", "--no-warnings", "--quiet"]


def download(url: str, cache_dir: str = "videos") -> str:
    """
    Download *url* to *cache_dir* and return the local file path.
    If the file already exists in the cache, skip the download and
    return the cached path immediately.
    """
    os.makedirs(cache_dir, exist_ok=True)

    # Ask yt-dlp what the output filename would be (without downloading)
    try:
        result = subprocess.run(
            _ydl_base_cmd() + [
                "--print", "filename",
                "-o", os.path.join(cache_dir, "%(title)s.%(ext)s"),
                "--no-playlist",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        predicted = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        predicted = ""

    if predicted and os.path.exists(predicted):
        return predicted

    # Download the video
    cmd = _ydl_base_cmd() + [
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", os.path.join(cache_dir, "%(title)s.%(ext)s"),
        url,
    ]
    subprocess.run(cmd, check=True, timeout=600)

    # Re-query the filename after download
    try:
        result = subprocess.run(
            _ydl_base_cmd() + [
                "--print", "filename",
                "-o", os.path.join(cache_dir, "%(title)s.%(ext)s"),
                "--no-playlist",
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        final = result.stdout.strip()
        if final and os.path.exists(final):
            return final
    except Exception:
        pass

    # Fallback: find the newest .mp4 in cache_dir
    mp4s = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir) if f.endswith(".mp4")]
    if mp4s:
        return max(mp4s, key=os.path.getmtime)

    raise FileNotFoundError(f"yt-dlp download succeeded but file not found for: {url}")


def expand_playlist(url: str) -> list[str]:
    """
    Expand a URL into individual video URLs.
    - Single video  → [url]
    - Playlist/channel → [url1, url2, ...]
    """
    try:
        result = subprocess.run(
            _ydl_base_cmd() + [
                "--flat-playlist",
                "--print", "webpage_url",
                url,
            ],
            capture_output=True, text=True, timeout=60,
        )
        urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if urls:
            return urls
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return [url]  # fallback: treat as single video

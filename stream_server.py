"""
stream_server.py
================
Streams the core Video-to-ASCII engine to the web via HTTP/WebSocket.
Dependencies: pip install fastapi uvicorn websockets yt-dlp

Priority Order:
  1. --playlist playlist.json  → JSON file (per-video vol, mode, path)
  2. --folder ./videos         → folder scan (filesystem order, not alphabetical)
  3. positional video arg      → single video (legacy behavior)

Media API endpoints (called by Sigil gui-server.js /api/media/* proxy):
  POST /api/enqueue   { url, mode, cols, vol, pixel, loop }
  POST /api/skip
  POST /api/stop
  POST /api/seek      { time }
  POST /api/volume    { vol }
  POST /api/loop      { enabled }
  GET  /api/status
  GET  /api/queue
"""

import asyncio
import subprocess
import json
import numpy as np
import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import os
from urllib.parse import urlparse
from websockets.exceptions import ConnectionClosed

# Import the existing engine (ascii_video_player2.py)
from ascii_video_player2 import VideoDecoder, AsciiMapper
from codec import encode_frame

app = FastAPI()


# ─────────────────────────────────────────────────────────
# URL RESOLUTION (yt-dlp)
# ─────────────────────────────────────────────────────────

def resolve_video_source(video: str) -> str:
    """
    Resolves a video source to something cv2.VideoCapture can open.

    Resolution order:
      1. If it looks like a URL and ends in a known container extension
         (e.g. .mp4, .webm) — return as-is; cv2 opens direct HTTP streams.
      2. If it looks like a URL to a platform (YouTube, Twitch, Twitter, etc.)
         — use yt-dlp to extract the best direct CDN stream URL.
      3. Otherwise treat as a local filesystem path (existing logic).
    """
    stripped = video.strip()

    if stripped.startswith(('http://', 'https://')):
        # Direct media URL — cv2 can open these without yt-dlp
        DIRECT_EXTS = ('.mp4', '.webm', '.mkv', '.avi', '.mov', '.m3u8')
        parsed_path = urlparse(stripped).path.lower()
        if any(parsed_path.endswith(ext) for ext in DIRECT_EXTS):
            return stripped

        # Platform URL — resolve via yt-dlp
        try:
            import yt_dlp
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(stripped, download=False)
                # For playlists, take first entry
                if 'entries' in info:
                    info = info['entries'][0]
                url = info.get('url') or info.get('manifest_url')
                if url:
                    print(f"[yt-dlp] Resolved: {stripped[:60]}... → CDN stream")
                    return url
        except ImportError:
            print("[yt-dlp] Not installed. Install with: pip install yt-dlp")
        except Exception as e:
            print(f"[yt-dlp] Resolution failed: {e}")

        # Fall through — let cv2 try the URL directly
        return stripped

    # Local filesystem path
    return resolve_video_path(stripped)


# ─────────────────────────────────────────────────────────
# MEDIA API CONTROL ENDPOINTS
# Called by Sigil’s gui-server.js /api/media/* proxy.
# ─────────────────────────────────────────────────────────

class EnqueueBody(BaseModel):
    url: str
    mode: int = 1
    cols: int | None = None
    vol: int = 1
    pixel: bool = False
    loop: bool = False

class SeekBody(BaseModel):
    time: float

class VolumeBody(BaseModel):
    vol: int

class LoopBody(BaseModel):
    enabled: bool


@app.post("/api/enqueue")
async def api_enqueue(body: EnqueueBody):
    """
    Add a video URL or path to the live queue.
    yt-dlp resolves platform URLs to CDN streams automatically.
    """
    resolved = resolve_video_source(body.url)
    is_pixel = body.pixel
    default_cols = body.cols if body.cols is not None else (450 if is_pixel else 200)

    entry = {
        "video": resolved,
        "mode":  max(1, min(5, body.mode)),
        "cols":  default_cols,
        "rows":  0,  # auto
        "vol":   max(0, min(5, body.vol)),
        "pixel": is_pixel,
    }

    queue = getattr(app.state, "queue", [])
    queue.append(entry)
    app.state.queue = queue
    if body.loop:
        app.state.loop = True

    pos = len(queue)
    print(f"[API] Enqueued #{pos}: {body.url[:80]}")
    return JSONResponse({"ok": True, "position": pos, "resolved": resolved, "entry": entry})


@app.post("/api/skip")
async def api_skip():
    """Signal the WebSocket loop to skip to the next video."""
    app.state._skip_requested = True
    return JSONResponse({"ok": True, "action": "skip"})


@app.post("/api/stop")
async def api_stop():
    """Stop playback and clear the queue."""
    app.state.queue = []
    app.state.current_index = 0
    app.state._skip_requested = True
    return JSONResponse({"ok": True, "action": "stop"})


@app.post("/api/seek")
async def api_seek(body: SeekBody):
    """Seek to a timestamp (seconds). Forwarded to active WebSocket via app.state."""
    if body.time < 0:
        return JSONResponse({"ok": False, "error": "time must be >= 0"}, status_code=400)
    app.state._seek_target = body.time
    return JSONResponse({"ok": True, "action": "seek", "time": body.time})


@app.post("/api/volume")
async def api_volume(body: VolumeBody):
    """Set volume level 0-5 for the current and future entries."""
    vol = max(0, min(5, body.vol))
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    if queue and idx < len(queue):
        queue[idx]["vol"] = vol
    app.state._volume_override = vol
    return JSONResponse({"ok": True, "vol": vol})


@app.post("/api/loop")
async def api_loop(body: LoopBody):
    """Toggle infinite loop mode."""
    app.state.loop = body.enabled
    return JSONResponse({"ok": True, "loop": body.enabled})


@app.get("/api/status")
async def api_status():
    """Return now-playing info for the Sigil /nowplaying command."""
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    loop  = getattr(app.state, "loop", False)
    entry = queue[idx] if queue and 0 <= idx < len(queue) else {}
    return JSONResponse({
        "ok":            True,
        "playing":       bool(entry),
        "current_index": idx,
        "queue_length":  len(queue),
        "loop":          loop,
        "video":         entry.get("video", ""),
        "mode":          entry.get("mode", 1),
        "vol":           entry.get("vol", 1),
        "pixel":         entry.get("pixel", False),
        "cols":          entry.get("cols", 200),
    })


@app.get("/api/queue")
async def api_queue():
    """Return the full current queue."""
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    return JSONResponse({
        "ok":            True,
        "current_index": idx,
        "queue":         queue,
    })


def get_video_dimensions(path: str) -> tuple[int, int]:
    """Quickly probe a video file to get (width, height) without decoding frames."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Could not open video file: {path!r}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h


def calc_auto_rows(cols: int, vid_w: int, vid_h: int, pixel_mode: bool) -> int:
    """
    Calculate rows from video aspect ratio.
    ASCII mode: characters are ~2x taller than wide, so divide by 2.
    Pixel mode: cells are square (CSS stretches), no correction needed.
    """
    ratio = vid_w / max(vid_h, 1)
    if pixel_mode:
        return max(1, round(cols / ratio))
    else:
        return max(1, round(cols / ratio / 2))

# Serve only whitelisted static files (security: prevents directory traversal)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_WHITELIST = {"app.js", "style.css", "codec.js"}

@app.get("/static/{filename}")
async def serve_static(filename: str):
    if filename not in STATIC_WHITELIST:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    filepath = os.path.join(BASE_DIR, filename)
    return FileResponse(filepath)

def get_html_content():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

def resolve_video_path(video: str) -> str:
    """
    Resolves a local video path by checking multiple locations in order:
      1. As-is (absolute or relative to CWD)
      2. Inside the project root (BASE_DIR)
      3. Inside BASE_DIR/videos/ subfolder
    Returns the first path that exists, or the original string if none found.
    """
    candidates = [
        video,
        os.path.join(BASE_DIR, video),
        os.path.join(BASE_DIR, "videos", os.path.basename(video)),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return video  # Return original; error will be caught during playback

def load_playlist(playlist_path: str) -> list[dict]:
    """Loads playlist from a JSON file and resolves all video paths."""
    with open(playlist_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    for item in items:
        item["video"] = resolve_video_source(item["video"])
    return items

def load_folder(folder_path: str, default_mode: int, default_vol: int) -> list[dict]:
    """
    Scans a folder for video files in filesystem order (top to bottom,
    as they appear in the directory — not alphabetically sorted).
    """
    supported = (".mp4", ".mkv", ".avi", ".mov", ".webm")
    entries = []
    with os.scandir(folder_path) as it:
        for entry in it:
            if entry.is_file() and entry.name.lower().endswith(supported):
                entries.append({
                    "video": entry.path,
                    "mode":  default_mode,
                    "vol":   default_vol
                })
    # Filesystem order (no sort applied)
    return entries

def build_queue(args) -> list[dict]:
    """
    Builds the video queue based on argument priority:
      1. --playlist JSON file
      2. --folder directory
      3. Single positional video argument
    """
    if args.playlist:
        print(f"[PLAYLIST] Loading: {args.playlist}")
        items = load_playlist(args.playlist)
        # Fill missing fields with global defaults
        for item in items:
            item.setdefault("mode", args.mode)
            item.setdefault("vol",  args.vol)
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
            item["cols"] = default_cols
            item["rows"] = args.rows
        return items

    # Legacy: single video argument (also supports URLs now)
    video_source = resolve_video_source(args.video)
    default_cols = args.cols if args.cols is not None else (450 if args.pixel else 200)
    return [{"video": video_source, "mode": args.mode, "vol": args.vol, "pixel": args.pixel, "cols": default_cols, "rows": args.rows}]


# ── APP STATE ────────────────────────────────────────────────────────
# Queue is stored in app.state so the WebSocket endpoint can read it.
# current_index tracks which video is playing.
# loop flag controls infinite playback.
# _skip_requested: set True by /api/skip and /api/stop to break WS loop.
# _seek_target: set by /api/seek; polled each frame by WS loop.
# ────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Serves the Frontend (HTML/JS/CSS) file to the client."""
    return HTMLResponse(get_html_content())


@app.get("/audio")
async def audio_stream(v: int | None = None, start: float = 0.0):
    """
    Extracts and streams audio from the currently active video entry.
    Server-side volume control via the entry's 'vol' field (0-5 scale).
      0 = Muted (FFmpeg never runs)
      1 = Normal (1.0x)
      5 = Double  (2.0x)
    Per-session: ?v=<index> selects which queue entry to serve audio for.
    """
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    if v is not None and 0 <= v < len(queue):
        idx = v
    entry = queue[idx] if queue and 0 <= idx < len(queue) else {}

    vol_level  = entry.get("vol", 1)
    video_path = entry.get("video", "video.mp4")

    # vol 0 → skip audio entirely, no FFmpeg process
    if vol_level <= 0:
        from fastapi import Response
        return Response(status_code=204)

    # For CDN stream URLs, check existence differently
    is_url = video_path.startswith(('http://', 'https://'))
    if not is_url and not os.path.exists(video_path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Video file not found")

    # Map 1-5 → 1.0x-2.0x FFmpeg volume
    ffmpeg_vol = 1.0 + (vol_level - 1) * 0.25

    async def audio_generator():
        ffmpeg_cmd = ["ffmpeg", "-nostdin"]
        if start > 0:
            ffmpeg_cmd.extend(["-ss", str(start)])
        ffmpeg_cmd.extend([
            "-i", video_path,
            "-vn",
            "-filter:a", f"volume={ffmpeg_vol}",
            "-acodec", "libmp3lame",
            "-ab", "128k",
            "-ar", "44100",
            "-f", "mp3",
            "-loglevel", "quiet",
            "pipe:1"
        ])
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    return StreamingResponse(
        audio_generator(),
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"}
    )


# ── Scrub-preview sprite (powers the hover thumbnails on the seek bar) ──
# A grid of small frames sampled across the video, like a YouTube preview strip.
# Built once per video on first request and kept in memory only (no disk cache).
_scrub_cache: dict = {}  # video_path -> {"meta": {...}, "jpeg": bytes} or None


def _build_scrub_sprite(video_path: str, max_count: int = 64, cell_w: int = 160):
    import math
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

    cell_h = max(1, round(cell_w * h0 / w0))
    n      = max(1, min(max_count, int(duration)))
    cols   = max(1, math.ceil(math.sqrt(n)))
    rows   = max(1, math.ceil(n / cols))
    interval = duration / n

    vf = f"fps={n}/{duration:.3f},scale={cell_w}:{cell_h},tile={cols}x{rows}"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", video_path, "-vf", vf,
             "-frames:v", "1", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg",
             "-loglevel", "error", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None

    return {
        "meta": {"available": True, "count": n, "gridCols": cols, "gridRows": rows,
                 "cellW": cell_w, "cellH": cell_h, "interval": interval, "duration": duration},
        "jpeg": proc.stdout,
    }


def _scrub_video_path(v: int | None) -> str:
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    if v is not None and 0 <= v < len(queue):
        idx = v
    entry = queue[idx] if queue and 0 <= idx < len(queue) else {}
    return entry.get("video", "")


@app.get("/scrub")
async def scrub_meta(v: int | None = None):
    from fastapi import Response
    import json as _json
    if not getattr(app.state, "thumbnails", True):
        return Response(content='{"available": false}', media_type="application/json")
    video_path = _scrub_video_path(v)
    if not video_path or not os.path.exists(video_path):
        return Response(content='{"available": false}', media_type="application/json")
    if video_path not in _scrub_cache:
        loop = asyncio.get_event_loop()
        _scrub_cache[video_path] = await loop.run_in_executor(None, _build_scrub_sprite, video_path)
    built = _scrub_cache.get(video_path)
    if not built:
        return Response(content='{"available": false}', media_type="application/json")
    meta = dict(built["meta"])
    meta["sprite"] = f"/scrub_sprite?v={v if v is not None else 0}"
    return Response(content=_json.dumps(meta), media_type="application/json")


@app.get("/scrub_sprite")
async def scrub_sprite(v: int | None = None):
    from fastapi import Response, HTTPException
    built = _scrub_cache.get(_scrub_video_path(v))
    if not built:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=built["jpeg"], media_type="image/jpeg")


def _origin_allowed(origin: str | None, host_header: str | None = None) -> bool:
    """Reject cross-site WebSocket hijacking while allowing localhost and LAN same-origin."""
    if not origin:
        return True
    try:
        origin_host = urlparse(origin).hostname
    except ValueError:
        return False
    if origin_host in {"localhost", "127.0.0.1"}:
        return True
    if host_header and origin_host == host_header.split(":")[0]:
        return True
    return False

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Streams ASCII frames for every video in the queue.
    Advances to the next entry automatically when a video ends.
    Loops back to the start if --loop is set.
    Responds to /api/skip and /api/seek signals via app.state flags.
    """
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin, websocket.headers.get("host")):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    adaptive = websocket.query_params.get("codec") == "adaptive"
    tolerance = getattr(app.state, "tolerance", 0)

    queue = getattr(app.state, "queue", [])
    loop  = getattr(app.state, "loop", False)

    if not queue:
        await websocket.send_text("Error: No video in queue!")
        await websocket.close()
        return

    queue_index = 0

    try:
        while True:
            # — Check for skip/stop signal —
            if getattr(app.state, "_skip_requested", False):
                app.state._skip_requested = False
                queue = getattr(app.state, "queue", [])
                if not queue:
                    break
                # /api/stop clears queue; /api/skip advances index
                queue_index = getattr(app.state, "current_index", 0) + 1

            queue = getattr(app.state, "queue", [])
            if not queue or queue_index >= len(queue):
                if loop and queue:
                    queue_index = 0
                else:
                    break

            entry      = queue[queue_index]
            video_path = entry["video"]
            render_mode= entry["mode"]
            pixel_mode = entry.get("pixel", False)
            cols       = entry.get("cols", 200)
            rows_cfg   = entry.get("rows", 0)

            app.state.current_index = queue_index

            print(f"[PLAYING] ({queue_index + 1}/{len(queue)}) {video_path[:80]}  "
                  f"mode={render_mode}  pixel={pixel_mode}  vol={entry['vol']}")

            try:
                vid_w, vid_h = get_video_dimensions(video_path)
            except FileNotFoundError:
                await websocket.send_text(f"Error: '{video_path}' not found!")
                queue_index += 1
                continue

            if rows_cfg == 0:
                rows = calc_auto_rows(cols, vid_w, vid_h, pixel_mode)
                print(f"[AUTO] {vid_w}x{vid_h} → grid {cols}x{rows}")
            else:
                rows = rows_cfg

            try:
                decoder = VideoDecoder(video_path, cols, rows, skip_gray=pixel_mode)
            except FileNotFoundError:
                await websocket.send_text(f"Error: '{video_path}' not found!")
                queue_index += 1
                continue

            mapper       = AsciiMapper()
            source_fps   = decoder.fps
            MAX_FPS      = 30
            char_byte_lut= np.array([ord(c) for c in mapper._lut], dtype=np.uint8)
            qb           = {5: 0, 4: 2, 3: 3, 2: 5}.get(render_mode, 0)

            if source_fps > MAX_FPS:
                skip_n = round(source_fps / MAX_FPS)
                effective_fps = source_fps / skip_n
            else:
                skip_n = 1
                effective_fps = source_fps
            frame_t = 1.0 / effective_fps

            duration = decoder.frame_count / decoder.fps if decoder.fps > 0 else 0
            await websocket.send_text(f"INIT:{effective_fps}:{render_mode}:{cols}:{rows}:{int(pixel_mode)}:{queue_index}:{duration:.3f}")
            if skip_n > 1:
                print(f"[FPS CAP] {source_fps} FPS → {effective_fps} FPS (skip every {skip_n} frames)")

            frame_buf = np.empty((rows, cols, 4), dtype=np.uint8) if render_mode > 1 else None

            import struct
            import time
            start_time = asyncio.get_event_loop().time()
            bw_start_time = time.time()
            bw_bytes_sent = 0
            bw_raw_bytes = 0
            debug_mode = getattr(app.state, "debug", False)
            frame_index = 0
            prev_frame = None

            if pixel_mode:
                pixel_send_buf = bytearray(4 + rows * cols * 3)
            elif render_mode > 1:
                ascii_send_buf = bytearray(4 + rows * cols * 4)

            cmd_queue = asyncio.Queue()
            is_paused = False

            async def receive_commands():
                try:
                    while True:
                        msg = await websocket.receive_json()
                        await cmd_queue.put(msg)
                except Exception:
                    pass

            receive_task = asyncio.create_task(receive_commands())

            raw_frame_num = 0

            def produce(pf, fi):
                for _ in range(skip_n - 1):
                    if not decoder.grab():
                        return None
                try:
                    gray_frame, bgr_frame = next(decoder)
                except StopIteration:
                    return None

                if pixel_mode:
                    raw_sz = 4 + rows * cols * 3
                    struct.pack_into(">I", pixel_send_buf, 0, fi)
                    pixel_send_buf[4:] = bgr_frame.tobytes()
                    buf = bytes(pixel_send_buf)
                    return ('bytes', buf, pf, raw_sz, len(buf))
                else:
                    indices = np.floor_divide(gray_frame, max(1, 256 // mapper._n))
                    np.clip(indices, 0, mapper._n - 1, out=indices)
                    if render_mode == 1:
                        char_matrix = mapper._lut[indices]
                        lines = [''.join(row) for row in char_matrix]
                        payload = f"{fi}\n" + '\n'.join(lines)
                        sz = len(payload.encode('utf-8'))
                        return ('text', payload, pf, sz, sz)
                    else:
                        char_codes = char_byte_lut[indices]
                        rgb = bgr_frame[:, :, ::-1]
                        if qb > 0:
                            rgb = (rgb >> qb) << qb
                        frame_buf[:, :, 0] = char_codes
                        frame_buf[:, :, 1:] = rgb
                        raw_sz = 4 + rows * cols * 4
                        if adaptive:
                            msg, npf = encode_frame(frame_buf.copy(), pf, fi, 3, tolerance)
                            return ('bytes', msg, npf, raw_sz, len(msg))
                        else:
                            struct.pack_into(">I", ascii_send_buf, 0, fi)
                            ascii_send_buf[4:] = frame_buf.tobytes()
                            buf = bytes(ascii_send_buf)
                            return ('bytes', buf, pf, raw_sz, len(buf))

            _loop = asyncio.get_event_loop()

            try:
                while True:
                    # — Poll API seek signal —
                    seek_target = getattr(app.state, "_seek_target", None)
                    if seek_target is not None:
                        app.state._seek_target = None
                        decoder.seek(seek_target)
                        prev_frame = None
                        frame_index = int(seek_target * effective_fps)
                        start_time = _loop.time() - (frame_index * frame_t)
                        bw_start_time = time.time()

                    # — Poll API skip signal —
                    if getattr(app.state, "_skip_requested", False):
                        break

                    while not cmd_queue.empty():
                        msg = cmd_queue.get_nowait()
                        if msg.get("type") == "pause":
                            is_paused = msg.get("paused", False)
                            if not is_paused:
                                start_time = _loop.time() - (frame_index * frame_t)
                                bw_start_time = time.time()
                        elif msg.get("type") == "seek":
                            target_sec = float(msg.get("time", 0))
                            decoder.seek(target_sec)
                            prev_frame = None
                            frame_index = int(target_sec * effective_fps)
                            start_time = _loop.time() - (frame_index * frame_t)
                            bw_start_time = time.time()

                    if is_paused:
                        await asyncio.sleep(0.1)
                        continue

                    result = await _loop.run_in_executor(None, produce, prev_frame, frame_index)

                    if result is None:
                        break

                    send_type, data, prev_frame, raw_size, wire_size = result

                    if send_type == 'text':
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)

                    bw_bytes_sent += wire_size
                    bw_raw_bytes += raw_size

                    current_time = time.time()
                    if debug_mode and current_time - bw_start_time >= 1.0:
                        raw_kbps = bw_raw_bytes / 1024
                        wire_kbps = bw_bytes_sent / 1024
                        ratio = raw_kbps / wire_kbps if wire_kbps > 0 else 0
                        print(f"[BW] RAW: {raw_kbps:.1f} KB/s | WIRE: {wire_kbps:.1f} KB/s | {ratio:.1f}x compression")
                        bw_start_time = current_time
                        bw_bytes_sent = 0
                        bw_raw_bytes = 0

                    elapsed = _loop.time() - start_time
                    wait = (frame_index * frame_t) - elapsed
                    if wait > 0:
                        await asyncio.sleep(wait)

                    frame_index += 1

            finally:
                receive_task.cancel()
                decoder.release()

            # Video finished → advance queue
            queue_index += 1
            queue = getattr(app.state, "queue", [])
            if queue_index >= len(queue):
                if loop:
                    print("[LOOP] Restarting queue from the beginning.")
                    queue_index = 0
                else:
                    print("[DONE] All videos finished.")
                    break

    except (WebSocketDisconnect, ConnectionClosed, RuntimeError):
        print("Client disconnected from the stream.")


ASCII_LOGO = "\033[36m" + r"""
    _    ____   ____ ___ _     ___ _   _ _____ 
   / \  / ___| / ___|_ _| |   |_ _| \ | | ____|
  / _ \ \___ \| |    | || |    | ||  \| |  _|  
 / ___ \ ___) | |___ | || |___ | || |\  | |___ 
/_/   \_\____/ \____|___|_____|___|_| \_|_____|
""" + "\033[0m"

HELP_TEXT = "\033[1;37m" + """
╔═══════════════════════════════════════════════════╗
║               ASCILINE  —  COMMANDS               ║
╠═══════════════════════════════════════════════════╣
║                                                   ║
║  \033[36m/help\033[1;37m      Show this help message               ║
║  \033[36m/status\033[1;37m    Show current server & playback info  ║
║  \033[36m/quit\033[1;37m      Stop the server and exit             ║
║                                                   ║
╠═══════════════════════════════════════════════════╣
║             CLI LAUNCH OPTIONS                    ║
╠═══════════════════════════════════════════════════╣
║                                                   ║
║  \033[33m─── Source ───\033[1;37m                                  ║
║  \033[32mvideo\033[1;37m          Path or URL to a video           ║
║  \033[32m--playlist\033[1;37m     JSON playlist file               ║
║  \033[32m--folder\033[1;37m       Play all videos in a folder      ║
║                                                   ║
║  \033[33m─── Render ───\033[1;37m                                  ║
║  \033[32m--mode\033[1;37m  \033[35m1-5\033[1;37m    Color quality                    ║
║     1=B&W  2=512c  3=32Kc  4=262Kc  5=16M        ║
║  \033[32m--pixel\033[1;37m        Pixel block mode (with mode 2-5) ║
║  \033[32m--cols\033[1;37m  \033[35mN\033[1;37m      Grid columns  (default: 200)     ║
║  \033[32m--rows\033[1;37m  \033[35mN\033[1;37m      Grid rows     (default: auto)    ║
║                                                   ║
║  \033[33m─── Playback ───\033[1;37m                                ║
║  \033[32m--vol\033[1;37m   \033[35m0-5\033[1;37m    Volume (0=mute, 1=normal, 5=2x)  ║
║  \033[32m--loop\033[1;37m         Loop the playlist infinitely     ║
║  \033[32m--quality\033[1;37m \033[35mlvl\033[1;37m  Codec quality (lossless,low,etc) ║
║                                                   ║
║  \033[33m─── Server ───\033[1;37m                                  ║
║  \033[32m--port\033[1;37m  \033[35mN\033[1;37m      Server port    (default: 8000)    ║
║  \033[32m--debug\033[1;37m        Show bandwidth stats (RAW/WIRE)  ║
║                                                   ║
╚═══════════════════════════════════════════════════╝
""" + "\033[0m"


def print_status():
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    loop  = getattr(app.state, "loop", False)
    cols  = getattr(app.state, "cols", 0)
    rows  = getattr(app.state, "rows", 0)
    print(f"\n\033[1;37m{'\u2550'*55}\033[0m")
    print(f" \033[32m\u25ba\033[0m \033[1mQueue\033[0m      : {len(queue)} video(s)")
    print(f" \033[32m\u25ba\033[0m \033[1mNow Playing\033[0m: {idx + 1}/{len(queue)}")
    if queue and idx < len(queue):
        entry = queue[idx]
        px = ' \033[35m[PIXEL]\033[0m' if entry.get('pixel') else ''
        cols = entry.get('cols', cols)
        rows = entry.get('rows', rows)
        print(f" \033[32m\u25ba\033[0m \033[1mVideo\033[0m      : \033[36m{entry['video'][:80]}\033[0m")
        print(f" \033[32m\u25ba\033[0m \033[1mSettings\033[0m   : mode={entry['mode']}{px} vol={entry['vol']}")
    res_str = f"{cols}x{rows}" if rows > 0 else f"{cols}x(auto)"
    print(f" \033[32m\u25ba\033[0m \033[1mResolution\033[0m : {res_str}")
    print(f" \033[32m\u25ba\033[0m \033[1mLoop\033[0m       : {'ON' if loop else 'OFF'}")
    print(f"\033[1;37m{'\u2550'*55}\033[0m\n")


def command_loop():
    print(f" \033[90mType \033[36m/help\033[90m for available commands.\033[0m\n")
    while True:
        try:
            cmd = input().strip().lower()
            if cmd in ('/help', 'help'):
                print(HELP_TEXT)
            elif cmd in ('/status', 'status'):
                print_status()
            elif cmd in ('/quit', 'quit', 'exit'):
                print("\n \033[33m\u23f9  Shutting down ASCILINE...\033[0m\n")
                os._exit(0)
            elif cmd:
                print(f" \033[90mUnknown command: '{cmd}'. Type \033[36m/help\033[90m for options.\033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n \033[33m\u23f9  Shutting down ASCILINE...\033[0m\n")
            os._exit(0)


if __name__ == "__main__":
    import argparse
    import os
    import threading

    os.system("")

    parser = argparse.ArgumentParser(
        description=f"{ASCII_LOGO}\nReal-Time ASCII Web Server\n"
                    "Stream local videos or URLs to your browser.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    src = parser.add_argument_group('\033[33mSource\033[0m')
    src.add_argument("video", nargs="?", default="video.mp4", help="Single video file or URL to stream")
    src.add_argument("--playlist", metavar="FILE", default=None)
    src.add_argument("--folder",   metavar="DIR",  default=None)

    render = parser.add_argument_group('\033[33mRender\033[0m')
    render.add_argument("--mode",  type=int, choices=[1,2,3,4,5], default=1)
    render.add_argument("--pixel", action="store_true", default=False)
    render.add_argument("--cols",  type=int, default=None)
    render.add_argument("--rows",  type=int, default=0)

    playback = parser.add_argument_group('\033[33mPlayback\033[0m')
    playback.add_argument("--vol",   type=int, default=1)
    playback.add_argument("--loop",  action="store_true", default=False)
    playback.add_argument("--quality", choices=["lossless","high","balanced","low"], default="lossless")
    playback.add_argument("--no-thumbnails", action="store_true", default=False)

    srv = parser.add_argument_group('\033[33mServer\033[0m')
    srv.add_argument("--host",  default="127.0.0.1")
    srv.add_argument("--port",  type=int, default=8000)
    srv.add_argument("--debug", action="store_true", default=False)

    args = parser.parse_args()

    if args.pixel and args.mode == 1:
        print("[ERROR] --pixel requires a color mode (--mode 2-5).")
        exit(1)
    if args.pixel and args.quality != "lossless":
        print("[ERROR] --pixel mode does not support the adaptive codec. Remove --quality.")
        exit(1)

    queue = build_queue(args)
    if not queue:
        print("[ERROR] No videos found.")
        exit(1)

    app.state.queue          = queue
    app.state.current_index  = 0
    app.state.loop           = args.loop
    app.state.tolerance      = {"lossless": 0, "high": 4, "balanced": 8, "low": 16}[args.quality]
    app.state.debug          = args.debug
    app.state.thumbnails     = not args.no_thumbnails
    app.state._skip_requested= False
    app.state._seek_target   = None
    app.state._volume_override = None
    global_default_cols      = args.cols if args.cols is not None else (450 if args.pixel else 200)
    app.state.cols           = global_default_cols
    app.state.rows           = args.rows

    # ── High FPS Warning ──
    high_fps_videos = []
    for entry in queue:
        vp = entry['video']
        if not vp.startswith(('http://', 'https://')):
            cap = cv2.VideoCapture(vp)
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                if fps > 35:
                    high_fps_videos.append((vp, fps))
            cap.release()

    if high_fps_videos:
        print("\n\033[1;33m[WARNING] High FPS Source(s) Detected:\033[0m")
        for vid, fps in high_fps_videos:
            print(f"  - \033[36m{vid}\033[0m is \033[1;31m{fps:.1f} FPS\033[0m")
        print("\033[33mASCILINE will automatically decimate to ~30 FPS.\033[0m\n")
        while True:
            choice = input("\033[1mContinue anyway? (y/n): \033[0m").strip().lower()
            if choice == 'y': break
            elif choice == 'n': exit(0)

    # ── Startup Banner ──
    print(ASCII_LOGO)
    print(f"\033[1;37m{'\u2550'*55}\033[0m")
    print(f" \033[32m\u25ba\033[0m \033[1mQueue\033[0m     : {len(queue)} video(s)")
    print(f" \033[32m\u25ba\033[0m \033[1mLoop\033[0m      : {'ON' if args.loop else 'OFF'}")
    res_str = f"{global_default_cols}x{args.rows}" if args.rows > 0 else f"{global_default_cols}x(auto)"
    print(f" \033[32m\u25ba\033[0m \033[1mResolution\033[0m: {res_str}")
    print(f" \033[32m\u25ba\033[0m \033[1mDefault\033[0m   : mode={args.mode} | pixel={'ON' if args.pixel else 'OFF'} | vol={args.vol}")
    print(f"\033[1;37m{'\u2500'*55}\033[0m")
    for i, entry in enumerate(queue, 1):
        px = ' \033[35m[PIXEL]\033[0m' if entry.get('pixel') else ''
        print(f"  {i:2}. \033[36m{entry['video'][:70]}\033[0m  (mode={entry['mode']}{px} vol={entry['vol']})")
    print(f"\033[1;37m{'\u2550'*55}\033[0m\n")
    print(f" \033[1;32m\U0001f680 Server live \u2192\033[0m \033[4;36mhttp://localhost:{args.port}\033[0m")
    print(f" \033[1;32m\U0001f4e1 Media API  \u2192\033[0m \033[4;36mhttp://localhost:{args.port}/api/status\033[0m\n")

    threading.Thread(target=command_loop, daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

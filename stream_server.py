"""
stream_server.py
================
Streams the core Video-to-ASCII engine to the web via HTTP/WebSocket.
Dependencies: pip install fastapi uvicorn websockets

Priority Order:
  1. --playlist playlist.json  → JSON file (per-video vol, mode, path)
  2. --folder ./videos         → folder scan (filesystem order, not alphabetical)
  3. positional video arg      → single video (legacy behavior)
"""

import asyncio
import subprocess
import json
import numpy as np
import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import os
from websockets.exceptions import ConnectionClosed

# Import the existing engine (ascii_video_player2.py)
from ascii_video_player2 import VideoDecoder, AsciiMapper

app = FastAPI()

# Serve static files (style.css, app.js) from the project directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")

def get_html_content():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

def resolve_video_path(video: str) -> str:
    """
    Resolves a video path by checking multiple locations in order:
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
        item["video"] = resolve_video_path(item["video"])
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
        return items

    if args.folder:
        print(f"[FOLDER] Scanning: {args.folder}")
        return load_folder(args.folder, args.mode, args.vol)

    # Legacy: single video argument
    return [{"video": args.video, "mode": args.mode, "vol": args.vol}]


# ── APP STATE ──────────────────────────────────────────────
# Queue is stored in app.state so the WebSocket endpoint can read it.
# current_index tracks which video is playing.
# loop flag controls infinite playback.
# ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Serves the Frontend (HTML/JS/CSS) file to the client."""
    return HTMLResponse(get_html_content())


@app.get("/audio")
async def audio_stream():
    """
    Extracts and streams audio from the currently active video entry.
    Server-side volume control via the entry's 'vol' field (0-5 scale).
      0 = Muted (FFmpeg never runs)
      1 = Normal (1.0x)
      5 = Double  (2.0x)
    """
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    entry = queue[idx] if queue else {}

    vol_level  = entry.get("vol", 1)
    video_path = entry.get("video", "video.mp4")

    # vol 0 → skip audio entirely, no FFmpeg process
    if vol_level <= 0:
        from fastapi import Response
        return Response(status_code=204)

    if not os.path.exists(video_path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Video file not found")

    # Map 1-5 → 1.0x-2.0x FFmpeg volume
    ffmpeg_vol = 1.0 + (vol_level - 1) * 0.25

    def audio_generator():
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-i", video_path,
                "-vn",
                "-filter:a", f"volume={ffmpeg_vol}",
                "-acodec", "libmp3lame",
                "-ab", "128k",
                "-ar", "44100",
                "-f", "mp3",
                "-loglevel", "quiet",
                "pipe:1"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            process.wait()

    return StreamingResponse(
        audio_generator(),
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"}
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Streams ASCII frames for every video in the queue.
    Advances to the next entry automatically when a video ends.
    Loops back to the start if --loop is set.
    """
    await websocket.accept()

    queue = getattr(app.state, "queue", [])
    loop  = getattr(app.state, "loop", False)
    cols  = getattr(app.state, "cols", 200)
    rows  = getattr(app.state, "rows", 80)

    if not queue:
        await websocket.send_text("Error: No video in queue!")
        await websocket.close()
        return

    queue_index = 0  # local index; advances through the queue

    try:
        while True:
            entry      = queue[queue_index]
            video_path = entry["video"]
            render_mode= entry["mode"]

            # IMPORTANT: Update current_index BEFORE sending INIT so that
            # when the client reloads /audio in response to INIT, the endpoint
            # already serves the correct video's audio.
            app.state.current_index = queue_index

            print(f"[PLAYING] ({queue_index + 1}/{len(queue)}) {video_path}  "
                  f"mode={render_mode}  vol={entry['vol']}")

            try:
                decoder = VideoDecoder(video_path, cols, rows)
            except FileNotFoundError:
                await websocket.send_text(f"Error: '{video_path}' not found!")
                queue_index += 1
                if queue_index >= len(queue):
                    if loop:
                        queue_index = 0
                    else:
                        break
                continue

            mapper       = AsciiMapper()
            fps          = decoder.fps
            frame_t      = 1.0 / fps
            char_byte_lut= np.array([ord(c) for c in mapper._lut], dtype=np.uint8)
            qb           = {5: 0, 4: 2, 3: 3, 2: 5}.get(render_mode, 0)

            await websocket.send_text(f"INIT:{fps}:{render_mode}:{cols}:{rows}")

            frame_buf = np.empty((rows, cols, 4), dtype=np.uint8) if render_mode > 1 else None

            try:
                for gray_frame, bgr_frame in decoder:
                    t0 = asyncio.get_event_loop().time()

                    indices = np.floor_divide(gray_frame, max(1, 256 // mapper._n))
                    np.clip(indices, 0, mapper._n - 1, out=indices)

                    if render_mode == 1:
                        char_matrix = mapper._lut[indices]
                        lines = [''.join(row) for row in char_matrix]
                        await websocket.send_text('\n'.join(lines))
                    else:
                        H, W = gray_frame.shape
                        char_codes = char_byte_lut[indices]
                        rgb = bgr_frame[:, :, ::-1]
                        if qb > 0:
                            rgb = (rgb >> qb) << qb
                        frame_buf[:, :, 0] = char_codes
                        frame_buf[:, :, 1:] = rgb
                        await websocket.send_bytes(frame_buf.tobytes())

                    elapsed = asyncio.get_event_loop().time() - t0
                    wait = frame_t - elapsed
                    if wait > 0:
                        await asyncio.sleep(wait)

            finally:
                decoder.release()

            # Video finished → advance queue
            queue_index += 1
            if queue_index >= len(queue):
                if loop:
                    print("[LOOP] Restarting queue from the beginning.")
                    queue_index = 0
                else:
                    print("[DONE] All videos finished.")
                    break

    except (WebSocketDisconnect, ConnectionClosed):
        print("Client disconnected from the stream.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Real-Time ASCII Web Server",
        formatter_class=argparse.RawTextHelpFormatter
    )

    # ── Source (mutually exclusive priority: playlist > folder > video) ──
    parser.add_argument(
        "video",
        nargs="?",
        default="video.mp4",
        help="Single video file to stream (legacy mode)"
    )
    parser.add_argument(
        "--playlist",
        metavar="FILE",
        default=None,
        help="Path to a playlist JSON file\n"
             "  Format: [{\"video\": \"a.mp4\", \"mode\": 5, \"vol\": 3}, ...]"
    )
    parser.add_argument(
        "--folder",
        metavar="DIR",
        default=None,
        help="Path to a folder; plays all videos in filesystem order"
    )

    # ── Playback ──
    parser.add_argument("--loop",  action="store_true", default=False, help="Loop the queue infinitely")
    parser.add_argument("--port",  type=int, default=8000, help="Server port (default: 8000)")

    # ── Global defaults (overridden per-entry in JSON) ──
    parser.add_argument(
        "--mode",
        type=int, choices=[1, 2, 3, 4, 5], default=1,
        help="Render mode: 1=B&W  2=512c  3=32Kc  4=262Kc  5=16M Ultra"
    )
    parser.add_argument("--cols", type=int, default=200, help="Column count (default: 200)")
    parser.add_argument("--rows", type=int, default=80,  help="Row count    (default: 80)")
    parser.add_argument(
        "--vol",
        type=int, default=1,
        help="Volume 0-5  (0=muted, 1=normal, 5=double) — global default"
    )

    args = parser.parse_args()

    # Build the queue
    queue = build_queue(args)

    if not queue:
        print("[ERROR] No videos found. Check your --playlist / --folder / video argument.")
        exit(1)

    # Save state
    app.state.queue         = queue
    app.state.current_index = 0
    app.state.loop          = args.loop
    app.state.cols          = args.cols
    app.state.rows          = args.rows

    # Summary
    print(f"\n{'='*50}")
    print(f"  ASCILINE  |  {len(queue)} video(s) in queue")
    print(f"  Loop      : {'ON' if args.loop else 'OFF'}")
    print(f"  Res       : {args.cols}x{args.rows}")
    print(f"  Default   : mode={args.mode}  vol={args.vol}")
    print(f"{'='*50}")
    for i, entry in enumerate(queue, 1):
        print(f"  {i:2}. {entry['video']}  [mode={entry['mode']} vol={entry['vol']}]")
    print(f"{'='*50}\n")
    print(f"Starting server → http://localhost:{args.port}\n")

    uvicorn.run(app, host="0.0.0.0", port=args.port, ws_ping_interval=None, ws_ping_timeout=None)

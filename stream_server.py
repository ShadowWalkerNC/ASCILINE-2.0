"""
stream_server.py
================
ASCILINE web server entry point.

This file is intentionally thin — all logic lives in the modules below:
  core/   — VideoDecoder, AsciiMapper, queue_manager, scrub
  api/    — FastAPI routes and Pydantic models
  cli/    — argparse, profiles, banner, command loop

Start the server:
  python stream_server.py video.mp4 --profile web
  python stream_server.py --folder videos --profile cinematic
  python stream_server.py --playlist playlist.json --loop
"""

import asyncio
import struct
import sys
import time
import threading
import os

import numpy as np
import cv2
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.responses import FileResponse
from websockets.exceptions import ConnectionClosed
from urllib.parse import urlparse

# ── Modular imports ───────────────────────────────────────────────────
from core.decoder import VideoDecoder, AsciiMapper
from core.queue_manager import build_queue, resolve_video_source
from core.scrub import build_scrub_sprite
from codec import encode_frame
from api.models import EnqueueBody, SeekBody, VolumeBody, LoopBody
from cli.args import build_arg_parser, command_loop, print_status, ASCII_LOGO
from cli.profiles import apply_profile

os.system("")  # Enable ANSI on Windows

# Detect headless / non-interactive environment (Docker, Render, CI, etc.)
HEADLESS = not sys.stdin.isatty()

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_WHITELIST = {"app.js", "style.css", "codec.js"}
_scrub_cache: dict = {}


# ─────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────

def get_video_dimensions(path: str) -> tuple[int, int]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Could not open video file: {path!r}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    return w, h


def calc_auto_rows(cols: int, vid_w: int, vid_h: int, pixel_mode: bool) -> int:
    ratio = vid_w / max(vid_h, 1)
    if pixel_mode:
        return max(1, round(cols / ratio))
    return max(1, round(cols / ratio / 2))


def _origin_allowed(origin: str | None, host_header: str | None = None) -> bool:
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


def _scrub_video_path(v: int | None) -> str:
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    if v is not None and 0 <= v < len(queue):
        idx = v
    entry = queue[idx] if queue and 0 <= idx < len(queue) else {}
    return entry.get("video", "")


# ─────────────────────────────────────────────────────────────────────
# STATIC / UI ROUTES
# ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    html_path = os.path.join(BASE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/static/{filename}")
async def serve_static(filename: str):
    from fastapi import HTTPException
    if filename not in STATIC_WHITELIST:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(os.path.join(BASE_DIR, filename))


# ─────────────────────────────────────────────────────────────────────
# MEDIA CONTROL API
# ─────────────────────────────────────────────────────────────────────

@app.post("/api/enqueue")
async def api_enqueue(body: EnqueueBody):
    resolved     = resolve_video_source(body.url)
    is_pixel     = body.pixel
    default_cols = body.cols if body.cols is not None else (450 if is_pixel else 200)
    entry = {
        "video": resolved,
        "mode":  max(1, min(5, body.mode)),
        "cols":  default_cols,
        "rows":  0,
        "vol":   max(0, min(5, body.vol)),
        "pixel": is_pixel,
    }
    queue = getattr(app.state, "queue", [])
    queue.append(entry)
    app.state.queue = queue
    if body.loop:
        app.state.loop = True
    print(f"[API] Enqueued #{len(queue)}: {body.url[:80]}")
    return JSONResponse({"ok": True, "position": len(queue), "resolved": resolved, "entry": entry})


@app.post("/api/skip")
async def api_skip():
    app.state._skip_requested = True
    return JSONResponse({"ok": True, "action": "skip"})


@app.post("/api/stop")
async def api_stop():
    app.state.queue = []
    app.state.current_index = 0
    app.state._skip_requested = True
    return JSONResponse({"ok": True, "action": "stop"})


@app.post("/api/seek")
async def api_seek(body: SeekBody):
    if body.time < 0:
        return JSONResponse({"ok": False, "error": "time must be >= 0"}, status_code=400)
    app.state._seek_target = body.time
    return JSONResponse({"ok": True, "action": "seek", "time": body.time})


@app.post("/api/volume")
async def api_volume(body: VolumeBody):
    vol = max(0, min(5, body.vol))
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    if queue and idx < len(queue):
        queue[idx]["vol"] = vol
    app.state._volume_override = vol
    return JSONResponse({"ok": True, "vol": vol})


@app.post("/api/loop")
async def api_loop(body: LoopBody):
    app.state.loop = body.enabled
    return JSONResponse({"ok": True, "loop": body.enabled})


@app.get("/api/status")
async def api_status():
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
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    return JSONResponse({"ok": True, "current_index": idx, "queue": queue})


@app.get("/api/meta")
async def api_meta():
    """Stream metadata for external integrations (dashboards, Discord bots, etc.)."""
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    entry = queue[idx] if queue and 0 <= idx < len(queue) else {}
    return JSONResponse({
        "ok":      True,
        "title":   os.path.basename(entry.get("video", "")),
        "mode":    entry.get("mode", 1),
        "pixel":   entry.get("pixel", False),
        "cols":    entry.get("cols", 200),
        "profile": getattr(app.state, "profile", None),
    })


# ─────────────────────────────────────────────────────────────────────
# AUDIO STREAM
# ─────────────────────────────────────────────────────────────────────

@app.get("/audio")
async def audio_stream(v: int | None = None, start: float = 0.0):
    queue = getattr(app.state, "queue", [])
    idx   = getattr(app.state, "current_index", 0)
    if v is not None and 0 <= v < len(queue):
        idx = v
    entry      = queue[idx] if queue and 0 <= idx < len(queue) else {}
    vol_level  = entry.get("vol", 1)
    video_path = entry.get("video", "video.mp4")

    if vol_level <= 0:
        return Response(status_code=204)

    is_url = video_path.startswith(("http://", "https://"))
    if not is_url and not os.path.exists(video_path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Video file not found")

    ffmpeg_vol = 1.0 + (vol_level - 1) * 0.25

    async def audio_generator():
        cmd = ["ffmpeg", "-nostdin"]
        if start > 0:
            cmd.extend(["-ss", str(start)])
        cmd.extend([
            "-i", video_path, "-vn",
            "-filter:a", f"volume={ffmpeg_vol}",
            "-acodec", "libmp3lame", "-ab", "128k",
            "-ar", "44100", "-f", "mp3",
            "-loglevel", "quiet", "pipe:1",
        ])
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while chunk := await process.stdout.read(4096):
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
        headers={"Accept-Ranges": "bytes"},
    )


# ─────────────────────────────────────────────────────────────────────
# SCRUB SPRITES
# ─────────────────────────────────────────────────────────────────────

@app.get("/scrub")
async def scrub_meta(v: int | None = None):
    import json as _json
    if not getattr(app.state, "thumbnails", True):
        return Response(content='{"available": false}', media_type="application/json")
    video_path = _scrub_video_path(v)
    if not video_path or not os.path.exists(video_path):
        return Response(content='{"available": false}', media_type="application/json")
    if video_path not in _scrub_cache:
        loop = asyncio.get_event_loop()
        _scrub_cache[video_path] = await loop.run_in_executor(
            None, build_scrub_sprite, video_path
        )
    built = _scrub_cache.get(video_path)
    if not built:
        return Response(content='{"available": false}', media_type="application/json")
    meta = dict(built["meta"])
    meta["sprite"] = f"/scrub_sprite?v={v if v is not None else 0}"
    return Response(content=_json.dumps(meta), media_type="application/json")


@app.get("/scrub_sprite")
async def scrub_sprite(v: int | None = None):
    from fastapi import HTTPException
    built = _scrub_cache.get(_scrub_video_path(v))
    if not built:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=built["jpeg"], media_type="image/jpeg")


# ─────────────────────────────────────────────────────────────────────
# WEBSOCKET — ASCII FRAME STREAM
# ─────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin, websocket.headers.get("host")):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    adaptive  = websocket.query_params.get("codec") == "adaptive"
    tolerance = getattr(app.state, "tolerance", 0)
    queue     = getattr(app.state, "queue", [])
    loop      = getattr(app.state, "loop", False)

    if not queue:
        await websocket.send_text("Error: No video in queue!")
        await websocket.close()
        return

    queue_index = 0

    try:
        while True:
            if getattr(app.state, "_skip_requested", False):
                app.state._skip_requested = False
                queue = getattr(app.state, "queue", [])
                if not queue:
                    break
                queue_index = getattr(app.state, "current_index", 0) + 1

            queue = getattr(app.state, "queue", [])
            if not queue or queue_index >= len(queue):
                if loop and queue:
                    queue_index = 0
                else:
                    break

            entry       = queue[queue_index]
            video_path  = entry["video"]
            render_mode = entry["mode"]
            pixel_mode  = entry.get("pixel", False)
            cols        = entry.get("cols", 200)
            rows_cfg    = entry.get("rows", 0)
            app.state.current_index = queue_index

            print(f"[PLAYING] ({queue_index+1}/{len(queue)}) {video_path[:80]}  "
                  f"mode={render_mode}  pixel={pixel_mode}  vol={entry['vol']}")

            try:
                vid_w, vid_h = get_video_dimensions(video_path)
            except FileNotFoundError:
                await websocket.send_text(f"Error: '{video_path}' not found!")
                queue_index += 1
                continue

            rows = calc_auto_rows(cols, vid_w, vid_h, pixel_mode) if rows_cfg == 0 else rows_cfg
            if rows_cfg == 0:
                print(f"[AUTO] {vid_w}x{vid_h} → grid {cols}x{rows}")

            try:
                decoder = VideoDecoder(video_path, cols, rows, skip_gray=pixel_mode)
            except FileNotFoundError:
                await websocket.send_text(f"Error: '{video_path}' not found!")
                queue_index += 1
                continue

            mapper        = AsciiMapper()
            source_fps    = decoder.fps
            MAX_FPS       = 30
            char_byte_lut = np.array([ord(c) for c in mapper._lut], dtype=np.uint8)
            qb            = {5: 0, 4: 2, 3: 3, 2: 5}.get(render_mode, 0)

            if source_fps > MAX_FPS:
                skip_n        = round(source_fps / MAX_FPS)
                effective_fps = source_fps / skip_n
            else:
                skip_n        = 1
                effective_fps = source_fps
            frame_t  = 1.0 / effective_fps
            duration = decoder.frame_count / decoder.fps if decoder.fps > 0 else 0

            await websocket.send_text(
                f"INIT:{effective_fps}:{render_mode}:{cols}:{rows}:{int(pixel_mode)}:{queue_index}:{duration:.3f}"
            )
            if skip_n > 1:
                print(f"[FPS CAP] {source_fps} FPS → {effective_fps} FPS (skip every {skip_n} frames)")

            frame_buf = np.empty((rows, cols, 4), dtype=np.uint8) if render_mode > 1 else None
            if pixel_mode:
                pixel_send_buf = bytearray(4 + rows * cols * 3)
            elif render_mode > 1:
                ascii_send_buf = bytearray(4 + rows * cols * 4)

            _loop         = asyncio.get_event_loop()
            start_time    = _loop.time()
            bw_start_time = time.time()
            bw_bytes_sent = bw_raw_bytes = 0
            debug_mode    = getattr(app.state, "debug", False)
            frame_index   = 0
            prev_frame    = None
            is_paused     = False
            cmd_queue     = asyncio.Queue()

            async def receive_commands():
                try:
                    while True:
                        msg = await websocket.receive_json()
                        await cmd_queue.put(msg)
                except Exception:
                    pass

            receive_task = asyncio.create_task(receive_commands())

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
                    return ("bytes", buf, pf, raw_sz, len(buf))
                else:
                    indices = np.floor_divide(gray_frame, max(1, 256 // mapper._n))
                    np.clip(indices, 0, mapper._n - 1, out=indices)
                    if render_mode == 1:
                        char_matrix = mapper._lut[indices]
                        lines   = ["".join(row) for row in char_matrix]
                        payload = f"{fi}\n" + "\n".join(lines)
                        sz      = len(payload.encode("utf-8"))
                        return ("text", payload, pf, sz, sz)
                    else:
                        char_codes = char_byte_lut[indices]
                        rgb = bgr_frame[:, :, ::-1]
                        if qb > 0:
                            rgb = (rgb >> qb) << qb
                        frame_buf[:, :, 0]  = char_codes
                        frame_buf[:, :, 1:] = rgb
                        raw_sz = 4 + rows * cols * 4
                        if adaptive:
                            msg, npf = encode_frame(frame_buf.copy(), pf, fi, 3, tolerance)
                            return ("bytes", msg, npf, raw_sz, len(msg))
                        else:
                            struct.pack_into(">I", ascii_send_buf, 0, fi)
                            ascii_send_buf[4:] = frame_buf.tobytes()
                            buf = bytes(ascii_send_buf)
                            return ("bytes", buf, pf, raw_sz, len(buf))

            try:
                while True:
                    seek_target = getattr(app.state, "_seek_target", None)
                    if seek_target is not None:
                        app.state._seek_target = None
                        decoder.seek(seek_target)
                        prev_frame  = None
                        frame_index = int(seek_target * effective_fps)
                        start_time  = _loop.time() - (frame_index * frame_t)
                        bw_start_time = time.time()

                    if getattr(app.state, "_skip_requested", False):
                        break

                    while not cmd_queue.empty():
                        msg = cmd_queue.get_nowait()
                        if msg.get("type") == "pause":
                            is_paused = msg.get("paused", False)
                            if not is_paused:
                                start_time    = _loop.time() - (frame_index * frame_t)
                                bw_start_time = time.time()
                        elif msg.get("type") == "seek":
                            t = float(msg.get("time", 0))
                            decoder.seek(t)
                            prev_frame  = None
                            frame_index = int(t * effective_fps)
                            start_time  = _loop.time() - (frame_index * frame_t)
                            bw_start_time = time.time()

                    if is_paused:
                        await asyncio.sleep(0.1)
                        continue

                    result = await _loop.run_in_executor(None, produce, prev_frame, frame_index)
                    if result is None:
                        break

                    send_type, data, prev_frame, raw_size, wire_size = result
                    if send_type == "text":
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)

                    bw_bytes_sent += wire_size
                    bw_raw_bytes  += raw_size
                    now = time.time()
                    if debug_mode and now - bw_start_time >= 1.0:
                        rk = bw_raw_bytes / 1024
                        wk = bw_bytes_sent / 1024
                        print(f"[BW] RAW: {rk:.1f} KB/s | WIRE: {wk:.1f} KB/s | {rk/wk if wk else 0:.1f}x")
                        bw_start_time = now
                        bw_bytes_sent = bw_raw_bytes = 0

                    elapsed = _loop.time() - start_time
                    wait    = (frame_index * frame_t) - elapsed
                    if wait > 0:
                        await asyncio.sleep(wait)
                    frame_index += 1

            finally:
                receive_task.cancel()
                decoder.release()

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


# ─────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = build_arg_parser()
    args   = parser.parse_args()

    # Apply profile before validation (profile sets defaults, flags override)
    if args.profile:
        apply_profile(args, args.profile)

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

    # ── High FPS warning (skipped in headless/cloud environments) ──
    if not HEADLESS:
        import cv2 as _cv2
        high_fps = []
        for entry in queue:
            vp = entry["video"]
            if not vp.startswith(("http://", "https://")):
                cap = _cv2.VideoCapture(vp)
                if cap.isOpened():
                    fps = cap.get(_cv2.CAP_PROP_FPS)
                    if fps > 35:
                        high_fps.append((vp, fps))
                cap.release()
        if high_fps:
            print("\n\033[1;33m[WARNING] High FPS Source(s) Detected:\033[0m")
            for vid, fps in high_fps:
                print(f"  - \033[36m{vid}\033[0m is \033[1;31m{fps:.1f} FPS\033[0m")
            print("\033[33mASCILINE will automatically decimate to ~30 FPS.\033[0m\n")
            while True:
                choice = input("\033[1mContinue anyway? (y/n): \033[0m").strip().lower()
                if choice == "y": break
                elif choice == "n": exit(0)
    else:
        print("[headless] Skipping high-FPS interactive check.")

    # ── Populate app.state ──
    global_default_cols       = args.cols if args.cols is not None else (450 if args.pixel else 200)
    app.state.queue           = queue
    app.state.current_index   = 0
    app.state.loop            = args.loop
    app.state.tolerance       = {"lossless": 0, "high": 4, "balanced": 8, "low": 16}[args.quality]
    app.state.debug           = args.debug
    app.state.thumbnails      = not args.no_thumbnails
    app.state.profile         = args.profile
    app.state._skip_requested = False
    app.state._seek_target    = None
    app.state._volume_override= None
    app.state.cols            = global_default_cols
    app.state.rows            = args.rows

    # ── Startup banner ──
    print(ASCII_LOGO)
    print(f"\033[1;37m{'═'*55}\033[0m")
    if args.profile:
        print(f" \033[32m►\033[0m \033[1mProfile\033[0m   : \033[35m{args.profile}\033[0m")
    print(f" \033[32m►\033[0m \033[1mQueue\033[0m     : {len(queue)} video(s)")
    print(f" \033[32m►\033[0m \033[1mLoop\033[0m      : {'ON' if args.loop else 'OFF'}")
    res_str = f"{global_default_cols}x{args.rows}" if args.rows > 0 else f"{global_default_cols}x(auto)"
    print(f" \033[32m►\033[0m \033[1mResolution\033[0m: {res_str}")
    print(f" \033[32m►\033[0m \033[1mDefault\033[0m   : mode={args.mode} | pixel={'ON' if args.pixel else 'OFF'} | vol={args.vol}")
    print(f"\033[1;37m{'─'*55}\033[0m")
    for i, entry in enumerate(queue, 1):
        px = ' \033[35m[PIXEL]\033[0m' if entry.get("pixel") else ""
        print(f"  {i:2}. \033[36m{entry['video'][:70]}\033[0m  (mode={entry['mode']}{px} vol={entry['vol']})")
    print(f"\033[1;37m{'═'*55}\033[0m\n")
    print(f" \033[1;32m🚀 Server live →\033[0m \033[4;36mhttp://localhost:{args.port}\033[0m")
    print(f" \033[1;32m📡 Media API  →\033[0m \033[4;36mhttp://localhost:{args.port}/api/status\033[0m\n")

    if not HEADLESS:
        threading.Thread(target=lambda: command_loop(app.state), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

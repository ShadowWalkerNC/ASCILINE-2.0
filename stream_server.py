"""
stream_server.py — ASCILINE web server.
Start-up: build_queue() blocks until remote URLs are on disk,
then uvicorn starts. WS always receives local file paths only.
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response, FileResponse
from websockets.exceptions import ConnectionClosed
from urllib.parse import urlparse

from core.decoder import VideoDecoder, AsciiMapper
from core.queue_manager import build_queue, resolve_video_source
from core.scrub import build_scrub_sprite
from codec import encode_frame
from api.models import EnqueueBody, SeekBody, VolumeBody, LoopBody
from api.upload import handle_upload
from cli.args import build_arg_parser, command_loop, print_status, ASCII_LOGO
from cli.profiles import apply_profile

os.system("")
HEADLESS = not sys.stdin.isatty()

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_WHITELIST = {"app.js", "style.css", "codec.js"}
_scrub_cache: dict = {}


# ─── helpers ────────────────────────────────────────────────────────────────

def get_video_dimensions(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Not on disk: {path!r}")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        raise FileNotFoundError(f"cv2 cannot open: {path!r}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if w == 0 or h == 0:
        raise FileNotFoundError(f"cv2 returned 0x0 for: {path!r}")
    return w, h


def calc_auto_rows(cols, vid_w, vid_h, pixel_mode):
    ratio = vid_w / max(vid_h, 1)
    return max(1, round(cols / ratio)) if pixel_mode else max(1, round(cols / ratio / 2))


def _origin_allowed(origin, host_header=None):
    if not origin:
        return True
    try:
        oh = urlparse(origin).hostname
    except ValueError:
        return False
    if oh in {"localhost", "127.0.0.1"}:
        return True
    if host_header and oh == host_header.split(":")[0]:
        return True
    return False


def _scrub_video_path(v):
    queue = getattr(app.state, "queue", [])
    idx = getattr(app.state, "current_index", 0)
    if v is not None and 0 <= v < len(queue):
        idx = v
    return (queue[idx] if queue and 0 <= idx < len(queue) else {}).get("video", "")


# ─── static ──────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/static/{filename}")
async def serve_static(filename):
    from fastapi import HTTPException
    if filename not in STATIC_WHITELIST:
        raise HTTPException(404)
    return FileResponse(os.path.join(BASE_DIR, filename))


# ─── API ────────────────────────────────────────────────────────────────────

@app.post("/api/enqueue")
async def api_enqueue(body: EnqueueBody):
    try:
        resolved = resolve_video_source(body.url)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    px = body.pixel
    cols = body.cols if body.cols else (450 if px else 200)
    entry = {"video": resolved, "mode": max(1, min(5, body.mode)),
             "cols": cols, "rows": 0, "vol": max(0, min(5, body.vol)), "pixel": px}
    q = getattr(app.state, "queue", [])
    q.append(entry)
    app.state.queue = q
    if body.loop:
        app.state.loop = True
    print(f"[API] queued #{len(q)}: {body.url[:80]}")
    return JSONResponse({"ok": True, "position": len(q), "resolved": resolved})


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    return await handle_upload(file, app.state)


@app.post("/api/skip")
async def api_skip():
    app.state._skip_requested = True
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def api_stop():
    app.state.queue = []
    app.state.current_index = 0
    app.state._skip_requested = True
    return JSONResponse({"ok": True})


@app.post("/api/seek")
async def api_seek(body: SeekBody):
    if body.time < 0:
        return JSONResponse({"ok": False, "error": "time>=0 required"}, status_code=400)
    app.state._seek_target = body.time
    return JSONResponse({"ok": True, "time": body.time})


@app.post("/api/volume")
async def api_volume(body: VolumeBody):
    vol = max(0, min(5, body.vol))
    q = getattr(app.state, "queue", [])
    idx = getattr(app.state, "current_index", 0)
    if q and idx < len(q):
        q[idx]["vol"] = vol
    app.state._volume_override = vol
    return JSONResponse({"ok": True, "vol": vol})


@app.post("/api/loop")
async def api_loop(body: LoopBody):
    app.state.loop = body.enabled
    return JSONResponse({"ok": True, "loop": body.enabled})


@app.get("/api/status")
async def api_status():
    q = getattr(app.state, "queue", [])
    idx = getattr(app.state, "current_index", 0)
    entry = q[idx] if q and 0 <= idx < len(q) else {}
    return JSONResponse({"ok": True, "playing": bool(entry), "current_index": idx,
                         "queue_length": len(q), "loop": getattr(app.state, "loop", False),
                         "video": entry.get("video", ""), "mode": entry.get("mode", 1),
                         "vol": entry.get("vol", 1), "pixel": entry.get("pixel", False),
                         "cols": entry.get("cols", 200)})


@app.get("/api/queue")
async def api_queue():
    q = getattr(app.state, "queue", [])
    return JSONResponse({"ok": True, "current_index": getattr(app.state, "current_index", 0), "queue": q})


@app.get("/api/meta")
async def api_meta():
    q = getattr(app.state, "queue", [])
    idx = getattr(app.state, "current_index", 0)
    entry = q[idx] if q and 0 <= idx < len(q) else {}
    return JSONResponse({"ok": True, "title": os.path.basename(entry.get("video", "")),
                         "mode": entry.get("mode", 1), "pixel": entry.get("pixel", False),
                         "cols": entry.get("cols", 200), "profile": getattr(app.state, "profile", None)})


# ─── audio ───────────────────────────────────────────────────────────────────

@app.get("/audio")
async def audio_stream(v: int | None = None, start: float = 0.0):
    q = getattr(app.state, "queue", [])
    idx = getattr(app.state, "current_index", 0)
    if v is not None and 0 <= v < len(q):
        idx = v
    entry = q[idx] if q and 0 <= idx < len(q) else {}
    vol_level = entry.get("vol", 1)
    vpath = entry.get("video", "")
    if vol_level <= 0 or not vpath or not os.path.exists(vpath):
        return Response(status_code=204)
    ffvol = 1.0 + (vol_level - 1) * 0.25

    async def gen():
        cmd = ["ffmpeg", "-nostdin"]
        if start > 0:
            cmd += ["-ss", str(start)]
        cmd += ["-i", vpath, "-vn", "-filter:a", f"volume={ffvol}",
                "-acodec", "libmp3lame", "-ab", "128k", "-ar", "44100",
                "-f", "mp3", "-loglevel", "quiet", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(*cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            while chunk := await proc.stdout.read(4096):
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    return StreamingResponse(gen(), media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})


# ─── scrub ──────────────────────────────────────────────────────────────────

@app.get("/scrub")
async def scrub_meta(v: int | None = None):
    import json as _j
    if not getattr(app.state, "thumbnails", True):
        return Response(content='{"available":false}', media_type="application/json")
    vp = _scrub_video_path(v)
    if not vp or not os.path.exists(vp):
        return Response(content='{"available":false}', media_type="application/json")
    if vp not in _scrub_cache:
        _scrub_cache[vp] = await asyncio.get_event_loop().run_in_executor(None, build_scrub_sprite, vp)
    built = _scrub_cache.get(vp)
    if not built:
        return Response(content='{"available":false}', media_type="application/json")
    meta = dict(built["meta"])
    meta["sprite"] = f"/scrub_sprite?v={v or 0}"
    return Response(content=_j.dumps(meta), media_type="application/json")


@app.get("/scrub_sprite")
async def scrub_sprite(v: int | None = None):
    from fastapi import HTTPException
    built = _scrub_cache.get(_scrub_video_path(v))
    if not built:
        raise HTTPException(404)
    return Response(content=built["jpeg"], media_type="image/jpeg")


# ─── websocket ─────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not _origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    adaptive  = websocket.query_params.get("codec") == "adaptive"
    tolerance = getattr(app.state, "tolerance", 0)
    loop      = getattr(app.state, "loop", False)

    # Wait up to 60 s for queue (covers first-deploy download time)
    for _ in range(600):
        if getattr(app.state, "queue", []):
            break
        await asyncio.sleep(0.1)

    # If still empty, send WAITING and hold connection for uploads
    if not getattr(app.state, "queue", []):
        await websocket.send_text("WAITING")
        try:
            while not getattr(app.state, "queue", []):
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, ConnectionClosed):
            return

    queue_index = 0
    try:
        while True:
            if getattr(app.state, "_skip_requested", False):
                app.state._skip_requested = False
                queue_index = getattr(app.state, "current_index", 0) + 1

            queue = getattr(app.state, "queue", [])
            if not queue or queue_index >= len(queue):
                if loop and queue:
                    queue_index = 0
                else:
                    # Wait for new uploads instead of closing
                    try:
                        while True:
                            queue = getattr(app.state, "queue", [])
                            if queue and queue_index < len(queue):
                                break
                            await asyncio.sleep(1.0)
                    except (WebSocketDisconnect, ConnectionClosed):
                        return
                    continue

            entry      = queue[queue_index]
            vpath      = entry["video"]
            mode       = entry["mode"]
            pixel_mode = entry.get("pixel", False)
            cols       = entry.get("cols", 200)
            rows_cfg   = entry.get("rows", 0)
            app.state.current_index = queue_index

            # Hard guard: file must exist on disk
            if not os.path.exists(vpath):
                print(f"[skip] missing file: {vpath}")
                await websocket.send_text(f"Error: file not found on disk")
                queue_index += 1
                continue

            print(f"[PLAYING] ({queue_index+1}/{len(queue)}) {vpath[:80]} mode={mode} vol={entry['vol']}")

            try:
                vid_w, vid_h = get_video_dimensions(vpath)
            except FileNotFoundError as e:
                print(f"[skip] {e}")
                await websocket.send_text(f"Error: cannot read video")
                queue_index += 1
                continue

            rows = calc_auto_rows(cols, vid_w, vid_h, pixel_mode) if rows_cfg == 0 else rows_cfg
            print(f"[AUTO] {vid_w}x{vid_h} → {cols}x{rows}")

            try:
                decoder = VideoDecoder(vpath, cols, rows, skip_gray=pixel_mode)
            except FileNotFoundError as e:
                print(f"[skip] {e}")
                queue_index += 1
                continue

            mapper = AsciiMapper()
            src_fps = decoder.fps
            MAX_FPS = 30
            char_byte_lut = np.array([ord(c) for c in mapper._lut], dtype=np.uint8)
            qb = {5: 0, 4: 2, 3: 3, 2: 5}.get(mode, 0)

            skip_n = round(src_fps / MAX_FPS) if src_fps > MAX_FPS else 1
            eff_fps = src_fps / skip_n
            frame_t = 1.0 / max(eff_fps, 1)
            duration = decoder.frame_count / decoder.fps if decoder.fps > 0 else 0

            await websocket.send_text(
                f"INIT:{eff_fps}:{mode}:{cols}:{rows}:{int(pixel_mode)}:{queue_index}:{duration:.3f}"
            )

            frame_buf = np.empty((rows, cols, 4), dtype=np.uint8) if mode > 1 else None
            if pixel_mode:
                pxbuf = bytearray(4 + rows * cols * 3)
            elif mode > 1:
                abuf = bytearray(4 + rows * cols * 4)

            el = asyncio.get_event_loop()
            t0 = el.time()
            bwt = time.time()
            bwb = bwr = 0
            dbg = getattr(app.state, "debug", False)
            fi = 0
            pf = None
            paused = False
            cq = asyncio.Queue()

            async def recv():
                try:
                    while True:
                        cq.put_nowait(await websocket.receive_json())
                except Exception:
                    pass

            rt = asyncio.create_task(recv())

            def produce(pf, fi):
                for _ in range(skip_n - 1):
                    if not decoder.grab():
                        return None
                try:
                    gf, bf = next(decoder)
                except StopIteration:
                    return None
                if pixel_mode:
                    struct.pack_into(">I", pxbuf, 0, fi)
                    pxbuf[4:] = bf.tobytes()
                    b = bytes(pxbuf)
                    return ("bytes", b, pf, 4+rows*cols*3, len(b))
                idx = np.floor_divide(gf, max(1, 256 // mapper._n))
                np.clip(idx, 0, mapper._n - 1, out=idx)
                if mode == 1:
                    lines = ["".join(row) for row in mapper._lut[idx]]
                    pay = f"{fi}\n" + "\n".join(lines)
                    sz = len(pay.encode())
                    return ("text", pay, pf, sz, sz)
                cc = char_byte_lut[idx]
                rgb = bf[:, :, ::-1]
                if qb > 0:
                    rgb = (rgb >> qb) << qb
                frame_buf[:, :, 0] = cc
                frame_buf[:, :, 1:] = rgb
                rsz = 4 + rows * cols * 4
                if adaptive:
                    msg, npf = encode_frame(frame_buf.copy(), pf, fi, 3, tolerance)
                    return ("bytes", msg, npf, rsz, len(msg))
                struct.pack_into(">I", abuf, 0, fi)
                abuf[4:] = frame_buf.tobytes()
                b = bytes(abuf)
                return ("bytes", b, pf, rsz, len(b))

            try:
                while True:
                    st = getattr(app.state, "_seek_target", None)
                    if st is not None:
                        app.state._seek_target = None
                        decoder.seek(st)
                        pf = None
                        fi = int(st * eff_fps)
                        t0 = el.time() - fi * frame_t
                        bwt = time.time()

                    if getattr(app.state, "_skip_requested", False):
                        break

                    while not cq.empty():
                        msg = cq.get_nowait()
                        if msg.get("type") == "pause":
                            paused = msg.get("paused", False)
                            if not paused:
                                t0 = el.time() - fi * frame_t
                                bwt = time.time()
                        elif msg.get("type") == "seek":
                            t = float(msg.get("time", 0))
                            decoder.seek(t)
                            pf = None
                            fi = int(t * eff_fps)
                            t0 = el.time() - fi * frame_t
                            bwt = time.time()

                    if paused:
                        await asyncio.sleep(0.1)
                        continue

                    res = await el.run_in_executor(None, produce, pf, fi)
                    if res is None:
                        break

                    stype, data, pf, rsz, wsz = res
                    if stype == "text":
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)

                    bwb += wsz
                    bwr += rsz
                    now = time.time()
                    if dbg and now - bwt >= 1.0:
                        print(f"[BW] {bwr/1024:.0f} raw / {bwb/1024:.0f} wire KB/s")
                        bwt, bwb, bwr = now, 0, 0

                    wait = fi * frame_t - (el.time() - t0)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    fi += 1

            finally:
                rt.cancel()
                decoder.release()

            queue_index += 1
            queue = getattr(app.state, "queue", [])
            if queue_index >= len(queue):
                if loop:
                    print("[LOOP] restart")
                    queue_index = 0
                else:
                    print("[DONE]")
                    break

    except (WebSocketDisconnect, ConnectionClosed, RuntimeError):
        print("[WS] client disconnected")


# ─── entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.profile:
        apply_profile(args, args.profile)
    if args.pixel and args.mode == 1:
        print("[ERROR] --pixel needs --mode 2-5")
        exit(1)
    if args.pixel and args.quality != "lossless":
        print("[ERROR] --pixel doesn't support adaptive codec")
        exit(1)

    print("[startup] Resolving sources...", flush=True)
    queue = build_queue(args)
    if not queue:
        print("[startup] Empty queue — server starts anyway, use Upload button.")

    if not HEADLESS and queue:
        high_fps = []
        for e in queue:
            cap = cv2.VideoCapture(e["video"])
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                if fps > 35:
                    high_fps.append((e["video"], fps))
            cap.release()
        if high_fps:
            print("\n[WARNING] High FPS detected:")
            for v, f in high_fps:
                print(f"  {v} @ {f:.0f} FPS")
            if input("Continue? (y/n): ").strip().lower() != "y":
                exit(0)
    else:
        print("[headless] skip FPS check")

    cols = args.cols if args.cols else (450 if args.pixel else 200)
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
    app.state.cols            = cols
    app.state.rows            = args.rows

    print(ASCII_LOGO)
    print(f" Queue: {len(queue)} | Loop: {args.loop} | Cols: {cols}")
    for i, e in enumerate(queue, 1):
        print(f"  {i}. {e['video'][:70]}  mode={e['mode']} vol={e['vol']}")
    print(f" http://localhost:{args.port}\n")

    if not HEADLESS:
        threading.Thread(target=lambda: command_loop(app.state), daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

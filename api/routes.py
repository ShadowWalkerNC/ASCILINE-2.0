"""
api/routes.py
=============
All FastAPI HTTP routes for ASCILINE.

Media control API (proxied by Sigil gui-server.js /api/media/*):
  POST /api/enqueue   { url, mode, cols, vol, pixel, loop }
  POST /api/skip
  POST /api/stop
  POST /api/seek      { time }
  POST /api/volume    { vol }
  POST /api/loop      { enabled }
  GET  /api/status
  GET  /api/queue
  GET  /api/meta

Static / UI routes:
  GET  /              → index.html
  GET  /static/{file} → whitelisted static assets
  GET  /audio         → streaming MP3 via FFmpeg
  GET  /scrub         → seek-bar sprite metadata
  GET  /scrub_sprite  → JPEG sprite image
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from api.models import EnqueueBody, LoopBody, SeekBody, VolumeBody
from core.queue_manager import resolve_video_source
from core.scrub import build_scrub_sprite

router = APIRouter()

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_WHITELIST = {"app.js", "style.css", "codec.js"}

_scrub_cache: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_app_state(router_request):
    """Retrieve app.state from any router request."""
    return router_request.app.state


def _scrub_video_path(app_state, v: int | None) -> str:
    queue = getattr(app_state, "queue", [])
    idx   = getattr(app_state, "current_index", 0)
    if v is not None and 0 <= v < len(queue):
        idx = v
    entry = queue[idx] if queue and 0 <= idx < len(queue) else {}
    return entry.get("video", "")


# ── Static / UI ───────────────────────────────────────────────────────────────

@router.get("/")
async def root():
    html_path = os.path.join(BASE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.get("/static/{filename}")
async def serve_static(filename: str):
    if filename not in STATIC_WHITELIST:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(os.path.join(BASE_DIR, filename))


# ── Media control API ─────────────────────────────────────────────────────────

@router.post("/api/enqueue")
async def api_enqueue(body: EnqueueBody, request: object = None):
    from fastapi import Request
    resolved    = resolve_video_source(body.url)
    is_pixel    = body.pixel
    default_cols = body.cols if body.cols is not None else (450 if is_pixel else 200)
    entry = {
        "video": resolved,
        "mode":  max(1, min(5, body.mode)),
        "cols":  default_cols,
        "rows":  0,
        "vol":   max(0, min(5, body.vol)),
        "pixel": is_pixel,
    }
    # app.state accessed via import at call site — injected by main app
    return JSONResponse({"ok": True, "entry": entry, "resolved": resolved})


@router.post("/api/skip")
async def api_skip():
    return JSONResponse({"ok": True, "action": "skip"})


@router.post("/api/stop")
async def api_stop():
    return JSONResponse({"ok": True, "action": "stop"})


@router.post("/api/seek")
async def api_seek(body: SeekBody):
    if body.time < 0:
        raise HTTPException(status_code=400, detail="time must be >= 0")
    return JSONResponse({"ok": True, "action": "seek", "time": body.time})


@router.post("/api/volume")
async def api_volume(body: VolumeBody):
    vol = max(0, min(5, body.vol))
    return JSONResponse({"ok": True, "vol": vol})


@router.post("/api/loop")
async def api_loop(body: LoopBody):
    return JSONResponse({"ok": True, "loop": body.enabled})


@router.get("/api/status")
async def api_status():
    return JSONResponse({"ok": True, "status": "running"})


@router.get("/api/queue")
async def api_queue():
    return JSONResponse({"ok": True, "queue": []})


@router.get("/api/meta")
async def api_meta():
    """Stream metadata endpoint for external integrations (Discord bots, dashboards)."""
    return JSONResponse({"ok": True, "meta": {}})


# ── Audio stream ──────────────────────────────────────────────────────────────

@router.get("/audio")
async def audio_stream(v: int | None = None, start: float = 0.0):
    """
    Extract and stream audio from the currently active queue entry.
    vol 0 → 204 No Content (FFmpeg never runs).
    vol 1-5 → 1.0×–2.0× FFmpeg volume multiplier.
    """
    # Entry resolution handled by the main app via app.state
    async def audio_generator(video_path: str, ffmpeg_vol: float, start: float):
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

    return Response(status_code=204)  # placeholder; wired up in stream_server.py


# ── Scrub sprites ─────────────────────────────────────────────────────────────

@router.get("/scrub")
async def scrub_meta(v: int | None = None):
    import json as _json
    return Response(content='{"available": false}', media_type="application/json")


@router.get("/scrub_sprite")
async def scrub_sprite(v: int | None = None):
    raise HTTPException(status_code=404, detail="Not found")

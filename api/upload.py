"""
api/upload.py
=============
Handles multipart file uploads. Saves to /tmp and enqueues immediately.
"""

import os
import uuid
from fastapi import UploadFile, File, Request
from fastapi.responses import JSONResponse

SUPPORTED = {".mp4", ".webm", ".mkv", ".avi", ".mov"}
MAX_SIZE  = 500 * 1024 * 1024  # 500 MB hard limit


async def handle_upload(file: UploadFile, app_state) -> JSONResponse:
    ext = os.path.splitext(file.filename or "")[-1].lower()
    if ext not in SUPPORTED:
        return JSONResponse(
            {"ok": False, "error": f"Unsupported file type: {ext}"},
            status_code=400,
        )

    safe_name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
    dest = os.path.join("/tmp", safe_name)

    size = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 256):  # 256 KB chunks
                size += len(chunk)
                if size > MAX_SIZE:
                    f.close()
                    os.unlink(dest)
                    return JSONResponse(
                        {"ok": False, "error": "File exceeds 500 MB limit"},
                        status_code=413,
                    )
                f.write(chunk)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    entry = {
        "video": dest,
        "mode":  3,
        "cols":  200,
        "rows":  0,
        "vol":   1,
        "pixel": False,
    }
    queue = getattr(app_state, "queue", [])
    queue.append(entry)
    app_state.queue = queue

    size_mb = size / (1024 * 1024)
    print(f"[upload] Saved {file.filename!r} → {dest} ({size_mb:.1f} MB), queue #{len(queue)}")
    return JSONResponse({"ok": True, "position": len(queue), "path": dest, "size_mb": round(size_mb, 1)})

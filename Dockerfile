# ─────────────────────────────────────────────
# ASCILINE — Render / Railway Dockerfile
# Includes FFmpeg, Python 3.11, all pip deps.
# ─────────────────────────────────────────────

FROM python:3.11-slim

# Install FFmpeg + OpenCV system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy full project
COPY . .

# Railway/Render inject PORT automatically
ENV PORT=8000
EXPOSE $PORT

# START_URL must be a direct .mp4 URL.
# Default: a tiny (~1 MB) public domain test clip for fast startup.
# Override with your own URL in Render → Environment → START_URL
CMD ["sh", "-c", "python stream_server.py ${START_URL:-https://www.w3schools.com/html/mov_bbb.mp4} --mode 3 --cols 200 --vol 1 --loop --host 0.0.0.0 --port ${PORT}"]

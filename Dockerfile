# ─────────────────────────────────────────────
# ASCILINE — Railway Dockerfile
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

# Railway injects PORT env var — fallback to 8000
ENV PORT=8000
EXPOSE $PORT

# Override START_URL in Railway env vars with your YouTube URL.
# Leave blank to use the default demo video.
CMD ["sh", "-c", "python stream_server.py ${START_URL:-https://www.youtube.com/watch?v=dQw4w9WgXcQ} --mode 3 --cols 200 --vol 1 --loop --host 0.0.0.0 --port ${PORT}"]

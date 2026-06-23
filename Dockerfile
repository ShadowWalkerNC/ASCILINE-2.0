# ─────────────────────────────────────────────
# ASCILINE — Render / Railway Dockerfile
# Includes FFmpeg, Python 3.11, all pip deps.
# Supports direct .mp4 URLs via cv2+FFmpeg network stack.
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

# START_URL must be a direct .mp4 URL (or any URL cv2+FFmpeg can stream).
# YouTube URLs will NOT work on cloud IPs without cookies.
# Use a direct mp4 link from Cloudinary, Bunny CDN, S3, GitHub Releases, etc.
#
# Example:
#   START_URL=https://your-cdn.com/video.mp4
#
# The default below is a public domain test clip (Big Buck Bunny, 60s).
CMD ["sh", "-c", "python stream_server.py ${START_URL:-https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4} --mode 3 --cols 200 --vol 1 --loop --host 0.0.0.0 --port ${PORT}"]

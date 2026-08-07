# ===== BUILDER STAGE =====
FROM docker.io/library/python:3.11-slim AS builder

WORKDIR /app

# static = fetch a modern static ffmpeg build (amd64 only, ~80MB, newer than Debian's).
# apt    = install Debian's ffmpeg (works on arm64, but drags in ~450MB of codec libs).
ARG FFMPEG_SOURCE=static

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    curl \
    xz-utils \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ffmpeg/ffprobe into /out. The host's ffmpeg is never executed — a bind mount only
# shares the filesystem, so the binary that runs is always the container's. Debian's
# 5.1 and Ubuntu's 4.4 both report Dolby Vision side data poorly, hence the static build.
RUN mkdir -p /out && \
    if [ "$FFMPEG_SOURCE" = "static" ]; then \
        curl -fsSL -o /tmp/ffmpeg.tar.xz \
          https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
        tar -xJf /tmp/ffmpeg.tar.xz -C /tmp && \
        cp /tmp/ffmpeg-*-amd64-static/ffmpeg /tmp/ffmpeg-*-amd64-static/ffprobe /out/ && \
        chmod +x /out/ffmpeg /out/ffprobe && \
        rm -rf /tmp/ffmpeg*; \
    else \
        apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
        cp "$(command -v ffmpeg)" "$(command -v ffprobe)" /out/ && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Frontend deps first so the node_modules layer survives source edits.
# (palworld-lens copies all of frontend/ before npm install, so its cache never hits.)
COPY frontend/package.json frontend/package-lock.jso[n] /app/frontend/
WORKDIR /app/frontend
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY frontend/ /app/frontend/
RUN npm run build

# ===== RUNTIME STAGE =====
FROM docker.io/library/python:3.11-slim

ARG DEV_MODE=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Same base image and Python version, so site-packages can be copied wholesale
# and build-essential/git stay out of the runtime image.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY --from=builder /out/ffmpeg /out/ffprobe /usr/local/bin/

COPY --from=builder /app/frontend/dist /usr/share/nginx/html/

COPY backend/ /app/backend/

COPY nginx.conf /etc/nginx/nginx.conf

COPY supervisor/ /tmp/supervisor/
RUN if [ "$DEV_MODE" = "true" ]; then \
    cp /tmp/supervisor/supervisord.dev.conf /etc/supervisor/conf.d/supervisord.conf; \
    else \
    cp /tmp/supervisor/supervisord.conf /etc/supervisor/conf.d/supervisord.conf; \
    fi \
    && rm -rf /tmp/supervisor

# Instance data (SQLite DB, posters, config). Mounted as a volume in compose.
RUN mkdir -p /app/data

EXPOSE 80

# Curl /api/health, NOT /health. The nginx-only /health returns 200 even when the
# backend is dead, so it never actually tests readiness.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost/api/health || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]

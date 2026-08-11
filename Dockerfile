FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl ffmpeg python3 python3-libtorrent python3-pip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend /frontend/node_modules/hls.js/dist/hls.mjs /app/vendor/hls.mjs
RUN python3 -m pip install --break-system-packages --no-cache-dir . \
    && groupadd --gid 10001 ofc \
    && useradd --uid 10001 --gid ofc --home-dir /nonexistent --shell /usr/sbin/nologin ofc \
    && mkdir -p /media /hls /resume /snapshots /subtitle-files \
    && chown -R ofc:ofc /media /hls /resume /snapshots
USER 10001:10001
EXPOSE 7100 7101 7102 7103

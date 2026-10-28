FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    DOWNLOAD_DIR=/downloads \
    HOME=/data/home \
    XDG_CACHE_HOME=/data/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md core.py spotify-dl.py ./
COPY web ./web

RUN pip install --no-cache-dir ".[web]"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -M -d /data/home \
        -s /usr/sbin/nologin appuser \
    && mkdir -p /data/logs /data/home /data/cache /downloads \
    && chown -R appuser:appuser /data /downloads

VOLUME ["/data", "/downloads"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

FROM python:3.12-slim-bookworm@sha256:e73ab8107cd24d9ccdad5c848794ab10d16b4e8462d88868ca840322841062dd
ARG TARGETARCH
ARG DEBIAN_MIRROR=https://deb.debian.org
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/state/runtime XDG_CACHE_HOME=/state/runtime/cache BAIDUPCS_GO_CONFIG_DIR=/state/baidu
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scripts/install_baidu.py /tmp/install_baidu.py
RUN python /tmp/install_baidu.py "${TARGETARCH}" && rm /tmp/install_baidu.py
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
CMD ["python", "app.py", "serve"]

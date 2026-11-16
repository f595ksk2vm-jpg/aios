# One image for every role: control plane, worker, CLI. The command decides which.
#   docker build -f deploy/Dockerfile -t aios .

FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 AIOS_DATA_DIR=/data
# git: workspace version history. util-linux (unshare): network-less sandbox for agent code.
# postgresql-client: pg_dump for backups when the database is PostgreSQL.
RUN apt-get update && apt-get install -y --no-install-recommends git util-linux postgresql-client nodejs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[postgres]"
COPY --from=ui /ui/dist ./ui/dist
# The app looks for the UI build next to the source tree; point it at the copy above.
ENV AIOS_UI_DIST=/app/ui/dist
RUN useradd --create-home --uid 10001 aios && mkdir -p /data && chown aios /data
USER aios
VOLUME /data
EXPOSE 8600
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/api/health', timeout=3)" || exit 1
CMD ["aios", "serve", "--host", "0.0.0.0", "--port", "8600"]

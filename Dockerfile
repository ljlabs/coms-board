FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/board \
    XDG_CACHE_HOME=/home/board/.cache \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_MCP_CONFIG=/app/playwright-cli.config.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && npm install --global --no-audit --no-fund opencode-ai@1.19.0 @playwright/cli@0.1.22 playwright@1.64.0-alpha-1790635538000 \
    && playwright install --with-deps chromium \
    && mkdir -p /app /data /workspace /home/board/playwright-output /home/board/.cache \
    && chown -R 10001:10001 /app /data /workspace /home/board \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chown=10001:10001 coms ./coms
COPY --chown=10001:10001 web ./web
COPY --chown=10001:10001 skills ./skills
COPY --chown=10001:10001 docker/playwright-cli.config.json ./playwright-cli.config.json
COPY --chown=10001:10001 coms.py docker_import.py docker_healthcheck.py ./
COPY --chown=10001:10001 README.md LICENSE ./
COPY --chmod=755 docker/board-entrypoint.sh /usr/local/bin/board-entrypoint

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/board-entrypoint"]
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python3", "/app/docker_healthcheck.py"]
CMD ["python3", "-m", "coms.server", "--host", "0.0.0.0", "--port", "8765"]

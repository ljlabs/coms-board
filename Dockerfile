FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/board

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --no-audit --no-fund opencode-ai \
    && mkdir -p /app /data /workspace /home/board \
    && chown -R 10001:10001 /app /data /workspace /home/board

WORKDIR /app
COPY --chown=10001:10001 coms ./coms
COPY --chown=10001:10001 web ./web
COPY --chown=10001:10001 skills ./skills
COPY --chown=10001:10001 coms.py docker_import.py docker_healthcheck.py ./
COPY --chown=10001:10001 README.md LICENSE ./

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python3", "/app/docker_healthcheck.py"]
CMD ["python3", "-m", "coms.server", "--host", "0.0.0.0", "--port", "8765"]

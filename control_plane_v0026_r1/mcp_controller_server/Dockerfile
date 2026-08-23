FROM python:3.14.6-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    MCP_CONTROLLER_CONFIG=/app/config/controller_config.json

LABEL org.opencontainers.image.title="automation-mcp-controller-server" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.revision="v0001-r1" \
      org.opencontainers.image.vendor="Automation Control Plane"

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --no-deps -r /app/requirements.txt

COPY src /app/src
COPY config /app/config
COPY VERSION RELEASE_REVISION /app/

RUN groupadd --gid 10001 mcpcontroller \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mcpcontroller \
    && mkdir -p /app/state \
    && chown -R 10001:10001 /app/state

USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["python", "-m", "mcp_controller_server.main"]
CMD ["http"]

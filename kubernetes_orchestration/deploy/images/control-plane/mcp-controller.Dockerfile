ARG PYTHON_RUNTIME_IMAGE
FROM ${PYTHON_RUNTIME_IMAGE}

ARG PYTHON_RUNTIME_IMAGE
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/python-cache \
    PYTHONPATH=/app/src \
    MCP_CONTROLLER_CONFIG=/app/config/controller_config.json

LABEL org.opencontainers.image.title="automation-mcp-controller-server" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.revision="v0001-r1" \
      org.opencontainers.image.base.name="${PYTHON_RUNTIME_IMAGE}"

WORKDIR /app
RUN printf '%s' "$PYTHON_RUNTIME_IMAGE" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --no-deps -r /app/requirements.txt \
    && python -m pip check

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

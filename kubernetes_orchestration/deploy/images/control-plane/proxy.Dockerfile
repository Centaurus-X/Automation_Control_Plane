ARG PYTHON_RUNTIME_IMAGE
FROM ${PYTHON_RUNTIME_IMAGE}

ARG PYTHON_RUNTIME_IMAGE
LABEL org.opencontainers.image.title="automation-proxy-gateway" \
      org.opencontainers.image.version="0.26.0" \
      org.opencontainers.image.revision="v0026-r1" \
      org.opencontainers.image.base.name="${PYTHON_RUNTIME_IMAGE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/python-cache \
    PSYCOPG_IMPL=binary \
    PYTHONPATH=/app/proxy_gateway:/app/proxy_gateway/src

RUN printf '%s' "$PYTHON_RUNTIME_IMAGE" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$' \
    && groupadd --gid 65532 automation \
    && useradd --uid 65532 --gid 65532 --home-dir /nonexistent --no-create-home automation

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY tools /app/tools
RUN install -d -m 0755 /opt/automation-proxy-wheelhouse \
    && pip download --disable-pip-version-check --only-binary=:all: \
       --dest /opt/automation-proxy-wheelhouse -r /app/requirements.txt \
    && python /app/tools/container_dependency_bootstrap.py \
       --write-wheelhouse-manifest /opt/automation-proxy-wheelhouse \
    && pip install --no-cache-dir --disable-pip-version-check --no-index \
       --find-links=/opt/automation-proxy-wheelhouse --only-binary=:all: \
       -r /app/requirements.txt \
    && python -m pip check \
    && python /app/tools/check_python_dependencies.py /app/requirements.txt \
       --expect psycopg-binary==3.3.4

COPY proxy_gateway /app/proxy_gateway
RUN install -d -o 65532 -g 65532 /app/proxy_gateway/_runtime \
    && python /app/tools/check_runtime_dependencies.py proxy \
    && python /app/tools/check_code_standards.py proxy \
       /app/proxy_gateway/src /app/proxy_gateway/test \
    && for test_file in /app/proxy_gateway/test/test_*.py; do \
         python "$test_file" || exit 1; \
       done

USER 65532:65532
WORKDIR /app/proxy_gateway
ENTRYPOINT ["python", "/app/tools/container_dependency_bootstrap.py", "--"]
CMD ["python", "-m", "gateway_main"]

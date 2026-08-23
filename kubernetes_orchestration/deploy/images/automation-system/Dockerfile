ARG PYTHON_RUNTIME_IMAGE
FROM ${PYTHON_RUNTIME_IMAGE}

ARG PYTHON_RUNTIME_IMAGE
ARG RUNTIME_ARCHIVE_SHA256
LABEL org.opencontainers.image.title="automation-system-soft-runtime" \
      org.opencontainers.image.version="0.101.0" \
      org.opencontainers.image.revision="v0101" \
      org.opencontainers.image.source-archive-sha256="${RUNTIME_ARCHIVE_SHA256}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/python-cache \
    PYTHONPATH=/opt/automation-system

COPY automation_system.zip /tmp/automation_system.zip

RUN printf '%s' "$PYTHON_RUNTIME_IMAGE" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$' \
    && test "$RUNTIME_ARCHIVE_SHA256" = "c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467" \
    && printf '%s  %s\n' "$RUNTIME_ARCHIVE_SHA256" /tmp/automation_system.zip | sha256sum -c - \
    && install -d -m 0755 /opt/runtime-extract \
    && python -m zipfile -e /tmp/automation_system.zip /opt/runtime-extract \
    && test -d /opt/runtime-extract/automation_system_v0101 \
    && mv /opt/runtime-extract/automation_system_v0101 /opt/automation-system \
    && rmdir /opt/runtime-extract \
    && cd /opt/automation-system \
    && sha256sum -c SHA256SUMS \
    && python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && python -m pip check \
    && groupadd --gid 65532 automation \
    && useradd --uid 65532 --gid 65532 --home-dir /nonexistent --no-create-home automation \
    && chown -R 65532:65532 /opt/automation-system \
    && chmod 0755 /opt/automation-system/run_worker.sh \
    && rm -f /tmp/automation_system.zip

USER 65532:65532
WORKDIR /opt/automation-system
ENTRYPOINT ["/opt/automation-system/run_worker.sh"]

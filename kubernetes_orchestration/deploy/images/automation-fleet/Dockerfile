ARG PYTHON_RUNTIME_IMAGE
FROM ${PYTHON_RUNTIME_IMAGE}

ARG PYTHON_RUNTIME_IMAGE
LABEL org.opencontainers.image.title="automation-fleet" \
      org.opencontainers.image.version="0.28.0" \
      org.opencontainers.image.revision="v0028-r1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/python-cache \
    PYTHONPATH=/opt/automation-fleet/src

COPY kubectl helm /usr/local/bin/
COPY src /opt/automation-fleet/src
COPY web /opt/automation-fleet/web
COPY config /opt/automation-fleet/config
COPY schemas /opt/automation-fleet/schemas
COPY component_descriptors /opt/automation-fleet/component_descriptors
COPY deploy/helm /opt/automation-fleet/deploy/helm
COPY VERSION RELEASE_REVISION /opt/automation-fleet/

RUN printf '%s' "$PYTHON_RUNTIME_IMAGE" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$' \
    && chmod 0755 /usr/local/bin/kubectl /usr/local/bin/helm \
    && /usr/local/bin/kubectl version --client=true \
    && /usr/local/bin/helm version --short \
    && groupadd --gid 65532 automation \
    && useradd --uid 65532 --gid 65532 --home-dir /nonexistent --no-create-home automation \
    && install -d -o 65532 -g 65532 -m 0700 /var/lib/automation-fleet

USER 65532:65532
WORKDIR /opt/automation-fleet
EXPOSE 9080
ENTRYPOINT ["python3", "-m", "automation_fleet.cli", "--root", "/opt/automation-fleet"]
CMD ["serve"]

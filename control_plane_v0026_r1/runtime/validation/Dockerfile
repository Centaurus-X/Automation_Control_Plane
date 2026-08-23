FROM python:3.14.6-slim-bookworm

ARG VALIDATION_FINGERPRINT=unversioned

LABEL org.opencontainers.image.title="automation-control-plane-validation" \
      org.opencontainers.image.version="0.26.0" \
      org.opencontainers.image.revision="v0026-r1" \
      org.opencontainers.image.validation-fingerprint="${VALIDATION_FINGERPRINT}"

ENV HOME=/tmp \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        diffutils \
        findutils \
        grep \
        openssl \
        procps \
        sed \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/automation-validation

COPY requirements-validation.txt ./requirements-validation.txt
COPY master_node/requirements.txt ./master_node/requirements.txt
COPY proxy_gateway/requirements.txt ./proxy_gateway/requirements.txt
COPY integration_test/e2e_v0025/requirements-e2e.txt \
     ./integration_test/e2e_v0025/requirements-e2e.txt
COPY proxy_gateway/tools/check_python_dependencies.py \
     ./proxy_gateway/tools/check_python_dependencies.py

RUN python -m pip install --no-cache-dir --only-binary=:all: \
        -r requirements-validation.txt \
    && python -m pip check \
    && python proxy_gateway/tools/check_python_dependencies.py \
        requirements-validation.txt --expect psycopg-binary==3.3.4

WORKDIR /release
CMD ["python", "--version"]

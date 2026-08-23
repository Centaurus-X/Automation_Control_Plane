FROM debian:bookworm-slim

ARG PYTHON_VERSION=3.14.6
ARG PYTHON_SOURCE_SHA256=143b1dddefaec3bd2e21e3b839b34a2b7fb9842272883c576420d605e9f30c63

LABEL org.opencontainers.image.title="automation-python-free-threaded" \
      org.opencontainers.image.version="3.14.6-v14" \
      org.opencontainers.image.python.free-threaded="true"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    LD_LIBRARY_PATH=/usr/local/lib \
    PYTHON_GIL=0 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# This image intentionally keeps the compiler and headers: derived release
# images can therefore build cp314t extension modules when no wheel exists.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libbz2-dev \
        libexpat1-dev \
        libffi-dev \
        libgdbm-dev \
        liblzma-dev \
        libncursesw5-dev \
        libpq-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        tk-dev \
        uuid-dev \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN curl --fail --location --proto '=https' --tlsv1.2 \
        --output Python.tar.xz \
        "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
    && echo "${PYTHON_SOURCE_SHA256}  Python.tar.xz" | sha256sum --check --strict - \
    && tar --extract --file Python.tar.xz \
    && cd "Python-${PYTHON_VERSION}" \
    && ./configure \
        --disable-gil \
        --enable-shared \
        --with-ensurepip=install \
        --with-system-expat \
        --with-system-ffi \
    && make -j "$(nproc)" \
    && make install \
    && cd / \
    && rm -rf "/usr/src/Python-${PYTHON_VERSION}" /usr/src/Python.tar.xz \
    && ln -sf /usr/local/bin/python3 /usr/local/bin/python \
    && ln -sf /usr/local/bin/pip3 /usr/local/bin/pip \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -c 'import sys,sysconfig; assert sys.version_info[:2] == (3,14); assert sysconfig.get_config_var("Py_GIL_DISABLED") == 1; assert sys._is_gil_enabled() is False'

WORKDIR /

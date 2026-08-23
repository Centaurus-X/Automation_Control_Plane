ARG PYTHON_RUNTIME_IMAGE=automation-python-free-threaded:3.14.6-v14
FROM ${PYTHON_RUNTIME_IMAGE}

LABEL org.opencontainers.image.title="automation-master-node" \
      org.opencontainers.image.version="0.26.0" \
      org.opencontainers.image.revision="v0026-r1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_GIL=0 \
    PYTHONPYCACHEPREFIX=/tmp/python-cache \
    PSYCOPG_IMPL=python \
    PYTHONPATH=/app/master_node:/app/master_node/src

RUN groupadd --gid 65532 automation \
    && useradd --uid 65532 --gid 65532 --home-dir /nonexistent --no-create-home automation

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt

COPY master_node /app/master_node
COPY tools /app/tools
RUN install -d -o 65532 -g 65532 /app/master_node/_runtime
RUN python -c 'import sys,sysconfig; assert sysconfig.get_config_var("Py_GIL_DISABLED") == 1; assert sys._is_gil_enabled() is False' \
    && python /app/tools/check_runtime_dependencies.py master \
    && python /app/tools/check_code_standards.py master \
       /app/master_node/src /app/master_node/test \
    && for test_file in /app/master_node/test/test_*.py; do \
         python "$test_file" || exit 1; \
       done

USER 65532:65532
WORKDIR /app/master_node
CMD ["python", "-m", "master_main"]

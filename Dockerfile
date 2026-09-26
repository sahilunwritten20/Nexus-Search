# syntax=docker/dockerfile:1
# Multi-stage: deps installed into a venv in the builder; the runtime image
# carries only the venv + app code (no compiler toolchain, no pip cache).

FROM python:3.12-slim AS builder

# torch ships a much smaller CPU-only wheel from PyTorch's index; without
# this pip pulls the multi-GB CUDA build.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    # production default: the API refuses to boot without NEXUS_API_KEY
    # (set it, or run with NEXUS_ENV=dev for local use)
    NEXUS_ENV=production \
    NEXUS_DB=/data/nexus_search.db

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY nexus_search ./nexus_search
COPY crawler_config.yaml ./

# SQLite state lives on a mounted volume (see docker-compose.yml)
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "nexus_search.core.api:app", "--host", "0.0.0.0", "--port", "8000"]

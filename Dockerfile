FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system ingestion && adduser --system --ingroup ingestion ingestion

COPY requirements.txt pyproject.toml README.md ./
# Same CPU-only torch install Dockerfile.worker already uses. On Linux the
# default PyPI wheel hard-depends on nvidia-cudnn-cu13, nvidia-cusparselt-cu13,
# nvidia-nccl-cu13, nvidia-nvshmem-cu13 and triton -- about 1.1 GB compressed,
# roughly 2 GB installed -- which is dead weight without a GPU. The embedder runs
# on CPU here, so nothing is given up.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.11.0
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check -r requirements.txt
COPY app ./app
COPY scripts ./scripts
RUN python -m pip install --disable-pip-version-check --no-deps .
RUN python -m scripts.generate_sbom /opt/project-intelligence-ingestion.cdx.json

USER ingestion
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/health', headers={'X-Forwarded-Proto':'https'}); urllib.request.urlopen(request, timeout=2)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

FROM python:3.13.1-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/home/rag/.cache/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system rag \
    && useradd --system --create-home --gid rag --shell /usr/sbin/nologin rag

WORKDIR /app

COPY requirements-runtime.lock ./requirements-runtime.lock
RUN pip install --no-cache-dir --requirement requirements-runtime.lock \
    && rm requirements-runtime.lock

COPY --chown=rag:rag app ./app
COPY --chown=rag:rag alembic ./alembic
COPY --chown=rag:rag alembic.ini ./alembic.ini

RUN mkdir -p /home/rag/.cache/huggingface \
    && chown -R rag:rag /home/rag/.cache

USER rag:rag

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

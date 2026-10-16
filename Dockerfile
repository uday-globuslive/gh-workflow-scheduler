FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DATABASE_URL=sqlite:////data/cronweb.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY cronweb ./cronweb

RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data \
    && chown app /data

USER app
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', timeout=4)"

CMD ["python", "-m", "cronweb"]

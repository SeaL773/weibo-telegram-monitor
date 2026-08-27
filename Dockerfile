FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium --with-deps \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app /app/data

COPY --chown=app:app main.py .
COPY --chown=app:app src/ src/

USER app

HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import os,sys,time; p='/tmp/healthcheck'; limit=int(os.getenv('HEALTH_TIMEOUT','1800')); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<limit else 1)"

CMD ["python", "-u", "main.py"]

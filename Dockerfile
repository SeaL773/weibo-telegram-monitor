FROM python:3.11-slim

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

CMD ["python", "-u", "main.py"]

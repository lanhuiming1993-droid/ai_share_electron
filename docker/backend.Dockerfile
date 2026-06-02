FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

RUN for attempt in 1 2 3; do \
      apt-get update \
      && apt-get install -y --no-install-recommends novnc openbox websockify x11vnc \
      && python -m playwright install-deps chromium \
      && python -m playwright install chromium \
      && break; \
      status=$?; \
      if [ "$attempt" -eq 3 ]; then exit "$status"; fi; \
      sleep 5; \
    done \
    && rm -rf /var/lib/apt/lists/*

COPY backend ./backend
COPY config ./config
COPY integrations ./integrations
COPY skills ./skills
COPY VERSION ./
COPY docker/backend-entrypoint.sh /usr/local/bin/alphadesk-backend-entrypoint

RUN chmod +x /usr/local/bin/alphadesk-backend-entrypoint \
    && mkdir -p /app/data

EXPOSE 8765 7900

CMD ["alphadesk-backend-entrypoint"]

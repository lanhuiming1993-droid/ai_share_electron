FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.cloud.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY backend ./backend
COPY config ./config
COPY integrations ./integrations
COPY skills ./skills
COPY VERSION ./
COPY docker/backend-cloud-entrypoint.sh /usr/local/bin/alphadesk-backend-cloud-entrypoint

RUN chmod +x /usr/local/bin/alphadesk-backend-cloud-entrypoint \
    && mkdir -p /app/data

EXPOSE 8765

CMD ["alphadesk-backend-cloud-entrypoint"]

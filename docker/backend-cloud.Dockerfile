FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=

COPY requirements.cloud.txt ./requirements.txt
RUN set -eux; \
    pip_args=""; \
    if [ -n "$PIP_INDEX_URL" ]; then pip_args="$pip_args -i $PIP_INDEX_URL"; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then pip_args="$pip_args --trusted-host $PIP_TRUSTED_HOST"; fi; \
    python -m pip install $pip_args --upgrade pip; \
    python -m pip install $pip_args -r requirements.txt

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

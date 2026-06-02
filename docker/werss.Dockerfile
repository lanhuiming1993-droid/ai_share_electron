FROM ghcr.io/rachelos/we-mp-rss@sha256:53912fcb3d523d1e640adcb7066cc18123f00e9510882a7982d0991f3113845f

COPY docker/patch_werss_runtime.py /tmp/patch_werss_runtime.py
COPY docker/sync_werss_admin.py /app/sync_werss_admin.py
COPY docker/werss-entrypoint.sh /app/alphadesk-entrypoint.sh

RUN python3 /tmp/patch_werss_runtime.py \
    && rm /tmp/patch_werss_runtime.py \
    && chmod +x /app/alphadesk-entrypoint.sh

ENTRYPOINT ["/app/alphadesk-entrypoint.sh"]

FROM ghcr.io/rachelos/we-mp-rss@sha256:53912fcb3d523d1e640adcb7066cc18123f00e9510882a7982d0991f3113845f

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN plant="/app/env_$(uname -m)" \
    && "$plant/bin/python3" -m playwright install --with-deps webkit

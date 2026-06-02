# Third-Party Notices

AlphaDesk uses third-party open-source software. Keep this file current when adding, removing, or upgrading external components.

## rachelos/we-mp-rss

- Project: [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)
- Purpose: WeChat Official Account login, subscriptions, article collection, and RSS generation.
- Integration: AlphaDesk builds a thin `alphadesk-werss` runtime image from the pinned upstream image. The wrapper installs the Playwright WebKit browser required by the upstream QR-login implementation. AlphaDesk does not vendor or copy the upstream application source.
- Upstream image: `ghcr.io/rachelos/we-mp-rss@sha256:53912fcb3d523d1e640adcb7066cc18123f00e9510882a7982d0991f3113845f`
- Wrapper: [`docker/werss.Dockerfile`](docker/werss.Dockerfile)
- License: MIT License
- License copy: [`LICENSES/we-mp-rss-MIT.txt`](LICENSES/we-mp-rss-MIT.txt)
- Upgrade policy: update the pinned digest only after reviewing upstream changes and passing QR login, subscription, RSS, persistence, and backup-restore smoke tests.

## Container Base Images

AlphaDesk builds its own images from:

- [`node:22-alpine`](https://hub.docker.com/_/node): Vue 3 build stage.
- [`nginx:1.28-alpine`](https://hub.docker.com/_/nginx): Web runtime.
- [`python:3.12-slim-bookworm`](https://hub.docker.com/_/python): FastAPI runtime.

These images contain their own third-party packages and license metadata. Release images should preserve the upstream license files shipped by their distributions.

## Application Dependencies

Python dependencies are declared in [`requirements.txt`](requirements.txt). JavaScript dependencies are declared in [`package.json`](package.json) and locked in [`package-lock.json`](package-lock.json).

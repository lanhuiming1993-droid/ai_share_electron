# Changelog

All notable changes to AlphaDesk are documented in this file.

The project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-06-02

### Added

- Docker Compose single-tenant delivery with `web`, `api`, and `werss` services.
- Nginx same-origin reverse proxy for Vue 3 and FastAPI.
- One-command initialization, startup, health, backup, update, and shutdown scripts.
- WeRSS QR-image proxy so internal Compose service names are not exposed to browsers.
- Thin WeRSS runtime image with the Playwright WebKit browser required by upstream QR login.
- GHCR image publishing and GitHub Release workflow.
- Third-party attribution and bundled MIT license text for `rachelos/we-mp-rss`.

### Changed

- Vue 3 browser client is now the only client.
- FastAPI data and log directories can be mounted with `ALPHADESK_DATA_DIR`.
- WeRSS runtime configuration can be injected by Compose environment variables.

### Removed

- Electron application source, npm dependency, and startup workflow.

[Unreleased]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/bdd21bb...v0.2.0

# Changelog

All notable changes to AlphaDesk are documented in this file.

The project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Loopback-only noVNC workspace for interactive Playwright channel login.
- WeRSS startup synchronization for deployment-level administrator credentials.

### Fixed

- Linux container crashes caused by Windows-only subprocess creation flags.
- WeRSS QR login when the upstream login page keeps long-lived network connections open.
- WeRSS upstream startup logs exposing deployment environment variables.
- MX HAR and other API failures returning HTML or plain text that the browser client attempted to parse as JSON.

## [0.2.5] - 2026-06-04

### Added

- IMA knowledge-base collection now follows `media_id` to fetch readable item content through `get_media_info`, and reads IMA note media through `get_doc_content`.
- IMA browsing now recurses into knowledge-base folders when no search query is provided.

### Changed

- IMA snapshots and report normalization now prefer fetched document content over search snippets while continuing to hide internal IMA IDs.

## [0.2.4] - 2026-06-04

### Fixed

- WeRSS queue maintenance now matches the upstream native console path under `/api/v1/wx/task-queue`.

## [0.2.3] - 2026-06-04

### Added

- WeRSS queue maintenance actions in the source configuration page for clearing article/content pending queues and histories.

### Fixed

- WeRSS manual backfill now accepts up to 100 pages and reports FastAPI validation errors as readable text instead of `[object Object]`.
- WeRSS queue clearing is exposed in AlphaDesk alongside manual backfill controls.

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

[Unreleased]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/v0.2.2...v0.2.3
[0.2.0]: https://github.com/lanhuiming1993-droid/ai_share_electron/compare/bdd21bb...v0.2.0

# Security Policy

## Sensitive local data

AlphaDesk is a local-first, single-tenant Docker Compose research workstation. API keys, channel tokens, browser profiles, HAR files, cookies, `.env`, logs and SQLite databases must remain outside Git history.

Before sharing diagnostics, use the in-app diagnostic bundle export. Do not attach the raw `data/` directory or browser profile.

## Reporting a vulnerability

Report vulnerabilities privately through the repository owner. Include the affected version, reproduction steps and impact. Do not open a public issue containing credentials, HAR contents or private-channel data.

## Repository rules

- Never commit plaintext credentials, `.env`, `data/`, backups or imported HAR files.
- Keep the default Web port bound to `127.0.0.1`. Do not expose the unauthenticated local-first API to a network.
- Never mount the Docker socket into the `api` container.
- Review Dependabot updates before merging.
- Require the `ci` workflow to pass before merging changes into `main`.
- Rotate any credential immediately if it is exposed in a commit, log, screenshot or issue.

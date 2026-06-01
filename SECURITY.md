# Security Policy

## Sensitive local data

AlphaDesk is a local-first research workstation. API keys, channel tokens, browser profiles, HAR files, cookies, logs and the SQLite database must remain outside Git history.

Before sharing diagnostics, use the in-app diagnostic bundle export. Do not attach the raw `data/` directory or browser profile.

## Reporting a vulnerability

Report vulnerabilities privately through the repository owner. Include the affected version, reproduction steps and impact. Do not open a public issue containing credentials, HAR contents or private-channel data.

## Repository rules

- Never commit plaintext credentials or imported HAR files.
- Review Dependabot updates before merging.
- Require the `ci` workflow to pass before merging changes into `main`.
- Rotate any credential immediately if it is exposed in a commit, log, screenshot or issue.

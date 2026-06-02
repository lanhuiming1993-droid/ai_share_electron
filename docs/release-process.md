# AlphaDesk Release Process

AlphaDesk releases use semantic version tags such as `v0.2.0`.

## Before Tagging

1. Update `VERSION`.
2. Move completed entries from `CHANGELOG.md` into the new version section.
3. Review `THIRD_PARTY_NOTICES.md`.
4. If the pinned WeRSS digest changes, verify upstream release notes, confirm the compatibility patch still applies, and run WeRSS QR login, subscription, RSS, persistence, and backup-restore smoke tests.
5. Run:

```powershell
.\.venv\Scripts\python.exe scripts\audit_public_repo.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
npm.cmd run build
.\scripts\init.cmd
docker compose --env-file .env config --quiet
```

Also verify that `http://127.0.0.1:7900/vnc.html` is reachable locally and remains bound to loopback only.

## Publish

Create and push an annotated tag:

```powershell
git tag -a v0.2.0 -m "AlphaDesk v0.2.0"
git push origin v0.2.0
```

The `release.yml` GitHub Actions workflow publishes:

- `ghcr.io/<owner>/alphadesk-web:<version>`
- `ghcr.io/<owner>/alphadesk-api:<version>`
- `ghcr.io/<owner>/alphadesk-werss:<version>`
- `latest` tags for stable releases
- `alphadesk-compose-<tag>.tar.gz`
- GitHub Release notes generated from merged changes

## Upgrade Existing Installations

Users should keep `.env`, `data/`, and `backups/` outside Git. Upgrade with:

```powershell
.\scripts\update.cmd
.\scripts\health.cmd
```

The update script creates a backup before pulling and restarting images.

## Rollback

1. Stop the instance.
2. Restore the backup archive containing `.env` and `data/`.
3. Set `ALPHADESK_VERSION` in `.env` to the previous image version.
4. Start the instance and run the health check.

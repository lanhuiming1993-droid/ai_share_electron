# AlphaDesk Enterprise Baseline

## Adopted now

### Standardized source registry

Inspired by OpenBB's provider and standard-model separation, `backend/source_registry.py` defines stable source identities, capabilities, credential modes and risk levels. Runtime channels still keep user-editable configuration in SQLite.

### Versioned database ledger

Inspired by Alembic's ordered migration model, `backend/db_migrations.py` records applied schema revisions in `schema_migrations`. The existing idempotent bootstrap remains in place for compatibility; future schema changes should add ordered migration steps before recording a new revision.

### Operational probes

Following common liveness and readiness conventions, the API exposes:

- `GET /health/live`: process-level liveness for Docker and reverse-proxy probes.
- `GET /health/ready`: SQLite schema, red-line policy, skills directory, collection worker and telemetry readiness.

### Optional OpenTelemetry export

JSONL logs remain the default local diagnostic path. Install `requirements-observability.txt` and set `ALPHADESK_OTEL_EXPORTER_OTLP_ENDPOINT` to export FastAPI and outbound `requests` spans to an OTLP collector.

### Repository governance

GitHub Actions validates backend tests, Python compilation, the Vue production build, Docker images, Compose configuration and a container smoke test. Dependabot checks Python, npm, Docker and GitHub Actions dependencies weekly.

## Evaluated for later

### Langfuse

Langfuse is useful for model traces, prompt management and evaluation. Its self-hosted deployment is intentionally not bundled into the default Compose package because it introduces a separate service stack. Add it as an optional remote observability integration when needed.

### Dedicated migration framework

If the SQLite schema grows beyond the current local-workstation scope, move the ledger to Alembic with SQLAlchemy models and generated revisions. The current ledger creates a controlled upgrade path without forcing a broad persistence rewrite.

### Durable job queue

The in-process collection worker is sufficient for one single-tenant Compose instance. A server edition should move jobs to a durable queue with explicit leases, retry policy and dead-letter handling.

## Primary references

- OpenBB custom provider development: https://docs.openbb.co/platform/development/contributing/provider_development
- OpenTelemetry Python instrumentation: https://opentelemetry.io/docs/languages/python/instrumentation/
- OpenTelemetry FastAPI instrumentation: https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html
- Alembic tutorial: https://alembic.sqlalchemy.org/en/latest/tutorial.html
- Kubernetes probes: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
- GitHub Actions Python CI: https://docs.github.com/actions/guides/building-and-testing-python
- GitHub Dependabot configuration: https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file
- Langfuse self-hosting: https://langfuse.com/self-hosting

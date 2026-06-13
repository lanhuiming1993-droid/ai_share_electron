# Cloud Agent Lite Deployment

This branch adds a cloud deployment profile for running AlphaDesk as a source
collection and report backend behind Hermes Agent.

## Scope

The cloud profile is intentionally limited to three sources:

- WeRSS official-account RSS, backed by `rachelos/we-mp-rss`
- ZSXQ MCP topics
- IMA OpenAPI knowledge base

The cloud backend does not start Xvfb, noVNC, or Playwright browser workspaces.

## Files

- `compose.cloud.yaml` - cloud runtime with `web`, `api`, and `werss`
- `docker/backend-cloud.Dockerfile` - slim FastAPI backend image
- `docker/backend-cloud-entrypoint.sh` - uvicorn-only entrypoint
- `requirements.cloud.txt` - runtime dependencies for the three-source cloud profile
- `deploy/cloud.env.example` - environment template, do not commit real secrets

## First Deploy

Create the server env file from the template:

```bash
mkdir -p deploy
cp deploy/cloud.env.example deploy/cloud.env
chmod 600 deploy/cloud.env
```

Fill in:

- `ALPHADESK_AGENT_TOKEN`
- `ALPHADESK_MODEL_API_KEY`
- `WERSS_PASSWORD`
- `WERSS_SECRET_KEY`
- `ALPHADESK_ZSXQ_MCP_URL`
- `ALPHADESK_IMA_CLIENT_ID`
- `ALPHADESK_IMA_API_KEY`

Start the stack:

```bash
docker compose --env-file deploy/cloud.env -f compose.cloud.yaml up -d --build
```

The default HTTP bind is loopback only:

```text
http://127.0.0.1:18080
```

This is intended for Hermes Agent running on the same server. Change
`ALPHADESK_HTTP_BIND` only when a reverse proxy and access control are ready.

## Hermes Agent API

Hermes should call the backend with:

```http
Authorization: Bearer ${ALPHADESK_AGENT_TOKEN}
```

Start a collection and report job:

```bash
curl -sS -X POST http://127.0.0.1:18080/api/agent/collect-report \
  -H "Authorization: Bearer ${ALPHADESK_AGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"lookback_days":30}'
```

Poll the returned job:

```bash
curl -sS http://127.0.0.1:18080/api/agent/jobs/<job_id> \
  -H "Authorization: Bearer ${ALPHADESK_AGENT_TOKEN}"
```

Fetch the report when `report_ready` is `true` or status is `review` /
`partial_review`:

```bash
curl -sS http://127.0.0.1:18080/api/agent/jobs/<job_id>/report \
  -H "Authorization: Bearer ${ALPHADESK_AGENT_TOKEN}"
```

Latest report:

```bash
curl -sS http://127.0.0.1:18080/api/agent/reports/latest \
  -H "Authorization: Bearer ${ALPHADESK_AGENT_TOKEN}"
```

## Suggested Hermes Instruction

When the user says "采集近30天数据并生成报告":

1. POST `/api/agent/collect-report` with `{"lookback_days": 30}`.
2. Poll `/api/agent/jobs/{job_id}` every 15 to 30 seconds.
3. When `report_ready` is true, fetch `/api/agent/jobs/{job_id}/report`.
4. Return a short summary and the HTML report content or link, depending on
   Hermes channel capabilities.

If the job status is `failed` or `report_failed`, return the job `error` and
ask the user to check source credentials or WeRSS login state.

## WeRSS Login

WeRSS still needs WeChat authorization. Use the existing web UI through a local
SSH tunnel or a protected reverse proxy, then open the WeRSS channel settings and
start QR login.

Example SSH tunnel from your workstation:

```bash
ssh -i ah.pem -L 18080:127.0.0.1:18080 <user>@<server-ip>
```

Then open:

```text
http://127.0.0.1:18080
```

#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-$(pwd)}"
PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
PROD_SERVICE="${PANDA_PROD_SERVICE:-panda-download}"
STAGING_SERVICE="${PANDA_STAGING_SERVICE:-panda-download-staging}"
WORKER_INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
WORKER_PORT="${PANDA_WORKER_PORT:-8865}"
WORKER_SECRET="${PANDA_WORKER_SECRET:-panda-dl-worker-token}"
RUN_CPU="${PANDA_RUN_CPU:-4}"
RUN_MEMORY="${PANDA_RUN_MEMORY:-16Gi}"
RUN_CONCURRENCY="${PANDA_RUN_CONCURRENCY:-8}"
JOB_WORKERS="${PANDA_JOB_WORKERS:-2}"

health_ok() {
  local url="$1"
  local health
  health="$(curl -fsS --max-time 20 "${url}/health" 2>/dev/null || true)"
  [[ -n "$health" ]] || return 1
  python3 - "$health" <<'PY'
import json,sys
try:
    d=json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
if d.get("status") != "ok": raise SystemExit(1)
if d.get("worker_configured") is not True: raise SystemExit(1)
if d.get("photo_download") is not True: raise SystemExit(1)
PY
}

deploy_service() {
  local service="$1"
  gcloud run deploy "$service" \
    --project "$PROJECT_ID" \
    --source "$SOURCE_DIR" \
    --region "$REGION" \
    --allow-unauthenticated \
    --execution-environment gen2 \
    --memory "$RUN_MEMORY" \
    --cpu "$RUN_CPU" \
    --concurrency "$RUN_CONCURRENCY" \
    --timeout 3600 \
    --min-instances 0 \
    --max-instances 1 \
    --cpu-boost \
    --no-cpu-throttling \
    --network=default \
    --subnet=default \
    --vpc-egress=private-ranges-only \
    --set-env-vars="PANDA_YT_WORKER_URL=http://${WORKER_IP}:${WORKER_PORT},PANDA_AUTH_REQUIRED=0,PANDA_COOKIE_SECURE=1,PANDA_PLAYLIST_LIMIT=100,PANDA_YT_WORKER_POLL=0.5,PANDA_JOB_WORKERS=${JOB_WORKERS},PANDA_JOB_TTL=7200,PANDA_INFO_TTL=300" \
    --set-secrets="PANDA_YT_WORKER_TOKEN=${WORKER_SECRET}:latest" \
    --quiet
}

gcloud config set project "$PROJECT_ID" >/dev/null
WORKER_IP="$(gcloud compute instances describe "$WORKER_INSTANCE" --zone "$WORKER_ZONE" --format='value(networkInterfaces[0].networkIP)')"
[[ -n "$WORKER_IP" ]] || { echo "Worker IP introuvable" >&2; exit 1; }

PREV_REV="$(gcloud run services describe "$PROD_SERVICE" --region "$REGION" --format='value(status.latestReadyRevisionName)' 2>/dev/null || true)"

echo "[autopilot] Deploy staging..."
deploy_service "$STAGING_SERVICE"
STAGING_URL="$(gcloud run services describe "$STAGING_SERVICE" --region "$REGION" --format='value(status.url)')"

for _ in $(seq 1 12); do
  if health_ok "$STAGING_URL"; then break; fi
  sleep 5
done
health_ok "$STAGING_URL" || { echo "[autopilot] Staging KO, prod inchangée." >&2; exit 1; }

echo "[autopilot] Staging OK: $STAGING_URL"

if [[ "${PANDA_AUTOPILOT_AUTO_DEPLOY:-0}" != "1" ]]; then
  echo "[autopilot] AUTO_DEPLOY=0: validation staging terminée, pas de promotion prod."
  exit 0
fi

echo "[autopilot] Promotion production..."
if ! deploy_service "$PROD_SERVICE"; then
  echo "[autopilot] Déploiement prod échoué." >&2
  exit 1
fi

gcloud run services update-traffic "$PROD_SERVICE" --region "$REGION" --to-latest --quiet >/dev/null
PROD_URL="$(gcloud run services describe "$PROD_SERVICE" --region "$REGION" --format='value(status.url)')"

for _ in $(seq 1 12); do
  if health_ok "$PROD_URL"; then
    echo "[autopilot] Production OK: $PROD_URL"
    exit 0
  fi
  sleep 5
done

echo "[autopilot] Production KO après déploiement." >&2
if [[ -n "$PREV_REV" ]]; then
  echo "[autopilot] Rollback vers $PREV_REV"
  gcloud run services update-traffic "$PROD_SERVICE" --region "$REGION" --to-revisions="${PREV_REV}=100" --quiet >/dev/null || true
fi
exit 1

#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
SERVICE="panda-download"
WORKER_INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
WORKER_PORT="${PANDA_WORKER_PORT:-8865}"
WORKER_SECRET="${PANDA_WORKER_SECRET:-panda-dl-worker-token}"
RUN_CPU="${PANDA_RUN_CPU:-4}"
RUN_MEMORY="${PANDA_RUN_MEMORY:-16Gi}"
RUN_CONCURRENCY="${PANDA_RUN_CONCURRENCY:-8}"
MIN_INSTANCES="${PANDA_MIN_INSTANCES:-0}"
JOB_WORKERS="${PANDA_JOB_WORKERS:-2}"
WORKER_JOBS="${PANDA_WORKER_JOBS:-2}"
WORKER_FRAGMENTS="${PANDA_WORKER_FRAGMENTS:-4}"

cd "$(dirname "$0")"
chmod +x setup-worker.sh

PANDA_WORKER_JOBS="$WORKER_JOBS" PANDA_WORKER_FRAGMENTS="$WORKER_FRAGMENTS" ./setup-worker.sh

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com compute.googleapis.com >/dev/null

WORKER_IP="$(gcloud compute instances describe "$WORKER_INSTANCE" --zone "$WORKER_ZONE" --format='value(networkInterfaces[0].networkIP)')"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding "$WORKER_SECRET" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null

echo "PANDA DOWNLOAD MAX -> worker privé http://${WORKER_IP}:${WORKER_PORT}"
echo "Cloud Run: ${RUN_CPU} CPU · ${RUN_MEMORY} · concurrency ${RUN_CONCURRENCY}"
echo "Worker: ${WORKER_JOBS} jobs · ${WORKER_FRAGMENTS} fragments/job"
echo "Auth YouTube legacy youtube-cookies: SUPPRIMEE"
echo "Auth active: Chromium worker + yt-dlp / gallery-dl"

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --execution-environment gen2 \
  --memory "$RUN_MEMORY" \
  --cpu "$RUN_CPU" \
  --concurrency "$RUN_CONCURRENCY" \
  --timeout 3600 \
  --min-instances "$MIN_INSTANCES" \
  --max-instances 1 \
  --cpu-boost \
  --no-cpu-throttling \
  --network=default \
  --subnet=default \
  --vpc-egress=private-ranges-only \
  --set-env-vars="PANDA_YT_WORKER_URL=http://${WORKER_IP}:${WORKER_PORT},PANDA_AUTH_REQUIRED=0,PANDA_COOKIE_SECURE=1,PANDA_PLAYLIST_LIMIT=100,PANDA_YT_WORKER_POLL=0.5,PANDA_JOB_WORKERS=${JOB_WORKERS},PANDA_JOB_TTL=7200,PANDA_INFO_TTL=300" \
  --set-secrets="PANDA_YT_WORKER_TOKEN=${WORKER_SECRET}:latest"

gcloud run services update-traffic "$SERVICE" \
  --region "$REGION" \
  --to-latest \
  --quiet >/dev/null

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
LATEST="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.latestReadyRevisionName)')"

echo
echo "===================================="
echo "PANDA DOWNLOAD · MAX ENGINE"
echo "$URL"
echo "Revision: $LATEST"
echo "===================================="

HEALTH="$(curl -fsS "$URL/health")"
echo "$HEALTH"

if ! echo "$HEALTH" | grep -q '3.0-max'; then
  echo "ERREUR: la révision MAX n'est pas active."
  exit 1
fi
if ! echo "$HEALTH" | grep -q '"worker_configured":true'; then
  echo "ERREUR: le worker yt-dlp/gallery-dl n'est pas configuré."
  exit 1
fi
if ! echo "$HEALTH" | grep -q '"photo_download":true'; then
  echo "ERREUR: le mode PHOTO n'est pas actif."
  exit 1
fi

echo
echo "OK: PANDA DOWNLOAD MAX est actif."
echo "VIDEO MP4 · AUDIO MP3 · PHOTO/GALERIE · PLAYLIST · TRACK EDITOR"

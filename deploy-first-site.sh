#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
SERVICE="panda-download"
WORKER_INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
WORKER_PORT="${PANDA_WORKER_PORT:-8865}"
WORKER_SECRET="${PANDA_WORKER_SECRET:-panda-dl-worker-token}"

cd "$(dirname "$0")"
chmod +x setup-worker.sh

# Keep the VM worker: this is the proven path that can read the persistent
# Chromium profile and run yt-dlp with the user's own authenticated session.
./setup-worker.sh

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com compute.googleapis.com >/dev/null

WORKER_IP="$(gcloud compute instances describe "$WORKER_INSTANCE" --zone "$WORKER_ZONE" --format='value(networkInterfaces[0].networkIP)')"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding "$WORKER_SECRET" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null

echo "PANDA DOWNLOAD -> worker privé http://${WORKER_IP}:${WORKER_PORT}"
echo "Auth YouTube legacy youtube-cookies: SUPPRIMEE"
echo "Auth YouTube active: Chromium worker + yt-dlp"

# IMPORTANT:
# --set-env-vars and --set-secrets REPLACE the old service configuration.
# This removes YTDLP_COOKIES_FILE and the old youtube-cookies secret mount.
# The first site is intentionally left without mandatory Google login for now:
# the priority is that downloads work.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --execution-environment gen2 \
  --memory 8Gi \
  --cpu 2 \
  --concurrency 4 \
  --timeout 3600 \
  --min-instances 0 \
  --max-instances 1 \
  --cpu-boost \
  --no-cpu-throttling \
  --network=default \
  --subnet=default \
  --vpc-egress=private-ranges-only \
  --set-env-vars="PANDA_YT_WORKER_URL=http://${WORKER_IP}:${WORKER_PORT},PANDA_AUTH_REQUIRED=0,PANDA_COOKIE_SECURE=1,PANDA_PLAYLIST_LIMIT=100,PANDA_YT_WORKER_POLL=0.8" \
  --set-secrets="PANDA_YT_WORKER_TOKEN=${WORKER_SECRET}:latest"

# Do not leave traffic on an old V6 revision that still contains the
# 'Mets à jour le secret youtube-cookies' message.
gcloud run services update-traffic "$SERVICE" \
  --region "$REGION" \
  --to-latest \
  --quiet >/dev/null

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
LATEST="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.latestReadyRevisionName)')"

echo
echo "===================================="
echo "PANDA DOWNLOAD · DIRECT WORKER"
echo "$URL"
echo "Revision: $LATEST"
echo "===================================="

HEALTH="$(curl -fsS "$URL/health")"
echo "$HEALTH"

if ! echo "$HEALTH" | grep -q '2.1-full-social'; then
  echo "ERREUR: l'ancien code est encore servi; la nouvelle revision n'est pas active."
  exit 1
fi
if ! echo "$HEALTH" | grep -q '"worker_configured":true'; then
  echo "ERREUR: le worker yt-dlp n'est pas configure dans Cloud Run."
  exit 1
fi

echo
echo "OK: panda-download utilise maintenant uniquement le worker Chromium + yt-dlp."
echo "L'ancien message youtube-cookies ne fait plus partie du chemin d'execution actif."

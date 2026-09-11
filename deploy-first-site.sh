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

# Refresh the authenticated private worker. This is the only YouTube auth path used
# by the first site: persistent Chromium profile + yt-dlp (+ gallery-dl for photos).
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

SECRET_MAP="PANDA_YT_WORKER_TOKEN=${WORKER_SECRET}:latest"
AUTH_REQUIRED=0

# Google login is enabled automatically when its three secrets already exist.
GOOGLE_CLIENT_SECRET="panda-dl-google-client-id"
GOOGLE_EMAILS_SECRET="panda-dl-allowed-emails"
SESSION_SECRET="panda-dl-session-secret"
if gcloud secrets describe "$GOOGLE_CLIENT_SECRET" >/dev/null 2>&1 \
  && gcloud secrets describe "$GOOGLE_EMAILS_SECRET" >/dev/null 2>&1 \
  && gcloud secrets describe "$SESSION_SECRET" >/dev/null 2>&1; then
  for secret in "$GOOGLE_CLIENT_SECRET" "$GOOGLE_EMAILS_SECRET" "$SESSION_SECRET"; do
    gcloud secrets add-iam-policy-binding "$secret" \
      --member="serviceAccount:${RUNTIME_SA}" \
      --role="roles/secretmanager.secretAccessor" \
      --quiet >/dev/null
  done
  SECRET_MAP+=",PANDA_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_SECRET}:latest,PANDA_ALLOWED_GOOGLE_EMAILS=${GOOGLE_EMAILS_SECRET}:latest,PANDA_SESSION_SECRET=${SESSION_SECRET}:latest"
  AUTH_REQUIRED=1
fi

echo "PANDA DOWNLOAD -> worker privé http://${WORKER_IP}:${WORKER_PORT}"
echo "Ancien secret youtube-cookies: désactivé pour ce service"

# --set-env-vars / --set-secrets replace the legacy configuration instead of preserving
# YTDLP_COOKIES_FILE or the old youtube-cookies mount. YouTube auth now falls through
# the private worker Chromium session.
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
  --set-env-vars="PANDA_YT_WORKER_URL=http://${WORKER_IP}:${WORKER_PORT},PANDA_AUTH_REQUIRED=${AUTH_REQUIRED},PANDA_COOKIE_SECURE=1,PANDA_PLAYLIST_LIMIT=100,PANDA_YT_WORKER_POLL=0.8" \
  --set-secrets="$SECRET_MAP"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "===================================="
echo "PANDA DOWNLOAD · PANDA DL ENGINE"
echo "$URL"
echo "===================================="
HEALTH="$(curl -fsS "$URL/health")"
echo "$HEALTH"

if echo "$HEALTH" | grep -q '2.1-full-social'; then
  echo "OK: ancien moteur youtube-cookies remplacé."
else
  echo "ATTENTION: la nouvelle révision ne semble pas active."
  exit 1
fi

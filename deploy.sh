#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
SERVICE="${PANDA_SERVICE:-panda-dl}"
WORKER_INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
WORKER_PORT="${PANDA_WORKER_PORT:-8865}"
WORKER_SECRET="${PANDA_WORKER_SECRET:-panda-dl-worker-token}"
GOOGLE_CLIENT_SECRET="panda-dl-google-client-id"
GOOGLE_EMAILS_SECRET="panda-dl-allowed-emails"
SESSION_SECRET="panda-dl-session-secret"

cd "$(dirname "$0")"
python3 -m py_compile app_full.py worker_server.py

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com compute.googleapis.com >/dev/null

for secret in "$WORKER_SECRET" "$GOOGLE_CLIENT_SECRET" "$GOOGLE_EMAILS_SECRET" "$SESSION_SECRET"; do
  if ! gcloud secrets describe "$secret" >/dev/null 2>&1; then
    echo "ERREUR: secret $secret introuvable."
    if [[ "$secret" == "$WORKER_SECRET" ]]; then
      echo "Lance: ./setup-worker.sh"
    else
      echo "Lance: ./setup-google-login.sh"
    fi
    exit 1
  fi
done

if ! gcloud compute instances describe "$WORKER_INSTANCE" --zone "$WORKER_ZONE" >/dev/null 2>&1; then
  echo "ERREUR: worker $WORKER_INSTANCE introuvable dans $WORKER_ZONE"
  exit 1
fi

WORKER_IP="$(gcloud compute instances describe "$WORKER_INSTANCE" --zone "$WORKER_ZONE" --format='value(networkInterfaces[0].networkIP)')"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for secret in "$WORKER_SECRET" "$GOOGLE_CLIENT_SECRET" "$GOOGLE_EMAILS_SECRET" "$SESSION_SECRET"; do
  gcloud secrets add-iam-policy-binding "$secret" --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
done

echo "PANDA DL -> worker privé http://${WORKER_IP}:${WORKER_PORT}"

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
  --update-env-vars="PANDA_YT_WORKER_URL=http://${WORKER_IP}:${WORKER_PORT},PANDA_AUTH_REQUIRED=1,PANDA_COOKIE_SECURE=1,PANDA_PLAYLIST_LIMIT=100" \
  --update-secrets="PANDA_YT_WORKER_TOKEN=${WORKER_SECRET}:latest,PANDA_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_SECRET}:latest,PANDA_ALLOWED_GOOGLE_EMAILS=${GOOGLE_EMAILS_SECRET}:latest,PANDA_SESSION_SECRET=${SESSION_SECRET}:latest"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "===================================="
echo "PANDA DL · FULL SOCIAL"
echo "$URL"
echo "===================================="
curl -fsS "$URL/health" || true
echo
echo "Google OAuth: ajoute cette origine dans Authorized JavaScript origins:"
echo "$URL"

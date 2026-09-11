#!/usr/bin/env bash
set -euo pipefail

SERVICE="${DINGO_SERVICE:-dingo-dl}"
REGION="${DINGO_REGION:-europe-west1}"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "❌ Aucun projet GCP actif. Lance: gcloud config set project TON_PROJECT_ID"
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null

echo "🔎 Projet: $PROJECT_ID"
echo "🔎 Service: $SERVICE ($REGION)"

gcloud services enable run.googleapis.com secretmanager.googleapis.com youtube.googleapis.com firestore.googleapis.com >/dev/null

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)' 2>/dev/null || true)"
if [[ -z "$URL" ]]; then
  echo "❌ Service Cloud Run '$SERVICE' introuvable dans $REGION. Déploie Dingo-dl d'abord."
  exit 1
fi

SPOTIFY_REDIRECT="$URL/api/sync/oauth/spotify/callback"
GOOGLE_REDIRECT="$URL/api/sync/oauth/youtube/callback"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "1) SPOTIFY"
echo "Redirect URI à enregistrer EXACTEMENT:"
echo "$SPOTIFY_REDIRECT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
read -r -p "Spotify Client ID: " SPOTIFY_CLIENT_ID
read -r -s -p "Spotify Client Secret: " SPOTIFY_CLIENT_SECRET
echo

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "2) GOOGLE / YOUTUBE"
echo "OAuth client: Web application"
echo "Redirect URI à enregistrer EXACTEMENT:"
echo "$GOOGLE_REDIRECT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
read -r -p "Google OAuth Client ID: " GOOGLE_CLIENT_ID
read -r -s -p "Google OAuth Client Secret: " GOOGLE_CLIENT_SECRET
echo

if [[ -z "$SPOTIFY_CLIENT_ID" || -z "$SPOTIFY_CLIENT_SECRET" || -z "$GOOGLE_CLIENT_ID" || -z "$GOOGLE_CLIENT_SECRET" ]]; then
  echo "❌ Une valeur OAuth est vide. Rien n'a été modifié."
  exit 1
fi

put_secret() {
  local name="$1"
  local value="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
  fi
}

put_secret "dingo-spotify-client-secret" "$SPOTIFY_CLIENT_SECRET"
put_secret "dingo-google-client-secret" "$GOOGLE_CLIENT_SECRET"
unset SPOTIFY_CLIENT_SECRET GOOGLE_CLIENT_SECRET

SA="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || true)"
if [[ -z "$SA" ]]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi

for SECRET in dingo-spotify-client-secret dingo-google-client-secret; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
done

gcloud run services update "$SERVICE" \
  --region "$REGION" \
  --update-env-vars="DINGO_PUBLIC_URL=$URL,DINGO_SPOTIFY_CLIENT_ID=$SPOTIFY_CLIENT_ID,DINGO_GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID" \
  --update-secrets="DINGO_SPOTIFY_CLIENT_SECRET=dingo-spotify-client-secret:latest,DINGO_GOOGLE_CLIENT_SECRET=dingo-google-client-secret:latest" \
  --quiet >/dev/null

echo
echo "✅ OAuth injecté dans Cloud Run."
echo "🎵 Dingo Sync: $URL/sync"
echo
echo "Spotify redirect URI:"
echo "$SPOTIFY_REDIRECT"
echo
echo "Google redirect URI:"
echo "$GOOGLE_REDIRECT"
echo
echo "État des providers:"
curl -fsS "$URL/api/sync/providers" | python3 -m json.tool || true

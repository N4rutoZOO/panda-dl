#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
CLIENT_ID="${1:-}"
EMAILS="${2:-}"

if [[ -z "$CLIENT_ID" ]]; then
  read -r -p "Google Web Client ID: " CLIENT_ID
fi
if [[ -z "$EMAILS" ]]; then
  read -r -p "Compte(s) Google autorisé(s), séparés par des virgules: " EMAILS
fi

if [[ -z "$CLIENT_ID" || "$CLIENT_ID" != *".apps.googleusercontent.com" ]]; then
  echo "ERREUR: utilise un OAuth Client ID Google de type Web application."
  exit 1
fi
if [[ -z "$EMAILS" ]]; then
  echo "ERREUR: indique au moins une adresse Google autorisée."
  exit 1
fi

SESSION_SECRET="$(openssl rand -hex 32)"
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable secretmanager.googleapis.com >/dev/null

put_secret(){
  local name="$1" value="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
  fi
}

put_secret panda-dl-google-client-id "$CLIENT_ID"
put_secret panda-dl-allowed-emails "$EMAILS"
put_secret panda-dl-session-secret "$SESSION_SECRET"

echo
echo "===================================="
echo "GOOGLE LOGIN READY"
echo "Client ID enregistré"
echo "Comptes autorisés: $EMAILS"
echo "===================================="
echo "Important: dans Google Cloud Console, ajoute l'origine HTTPS de PANDA DL aux Authorized JavaScript origins du même Web Client ID."

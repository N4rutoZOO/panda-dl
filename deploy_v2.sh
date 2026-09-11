#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-project-017b13a1-e57e-4723-a2b}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-dingo-dl}"

printf '\n=== DINGO SYNC V2 · LOCAL VERIFY x5 ===\n'
for i in 1 2 3 4 5; do
  printf 'Local check %s/5... ' "$i"
  python3 -m compileall -q .
  python3 verify_v2.py >/tmp/dingo-v2-verify.json
  echo OK
done

printf '\n=== DEPLOY CLOUD RUN ===\n'
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
[ -n "$URL" ] || { echo 'FAIL: Cloud Run URL vide'; exit 1; }

printf '\n=== REMOTE VERIFY x5 ===\n'
for i in 1 2 3 4 5; do
  printf 'Remote check %s/5... ' "$i"

  code="$(curl -fsS -o /tmp/dingo-sync.html -w '%{http_code}' "$URL/sync")"
  [ "$code" = "200" ]
  grep -q 'Dingo-dl Sync V2' /tmp/dingo-sync.html
  grep -q 'IMPORT FICHIER / TEXTE' /tmp/dingo-sync.html

  curl -fsS "$URL/api/sync/providers" > /tmp/dingo-providers.json
  curl -fsS "$URL/api/sync/storage" > /tmp/dingo-storage.json
  curl -fsS "$URL/openapi.json" > /tmp/dingo-openapi.json

  python3 - <<'PY'
import json
from pathlib import Path

providers = json.loads(Path('/tmp/dingo-providers.json').read_text())['providers']
by_id = {p['id']: p for p in providers}
for pid in ('spotify','youtube','deezer','apple_music','tidal','soundcloud'):
    assert pid in by_id, f'missing provider: {pid}'
for pid in ('deezer','apple_music','tidal','soundcloud'):
    assert by_id[pid].get('status') == 'coming_soon', f'{pid} status'

storage = json.loads(Path('/tmp/dingo-storage.json').read_text())
assert storage.get('firestore_ready') is True, storage
assert storage.get('secure_token_storage') is True, storage
assert storage.get('token_key_configured') is True, storage
assert storage.get('last_error') in (None, ''), storage

paths = json.loads(Path('/tmp/dingo-openapi.json').read_text())['paths']
required = (
    '/sync',
    '/api/sync/providers',
    '/api/sync/storage',
    '/api/sync/history',
    '/api/sync/transfer',
    '/api/sync/import/youtube',
    '/api/sync/jobs/{job_id}',
    '/api/sync/jobs/{job_id}/candidates/{index}',
    '/api/sync/jobs/{job_id}/resolve/{index}',
)
for route in required:
    assert route in paths, f'missing route: {route}'
PY

  echo OK
  sleep 1
done

printf '\n✅ DINGO SYNC V2 VALIDÉ 5/5\n'
printf 'SITE: %s\n' "$URL"
printf 'SYNC: %s/sync\n' "$URL"
printf 'Connecteurs actifs: Spotify, YouTube / YouTube Music\n'
printf 'Connecteurs à venir: Deezer, Apple Music, TIDAL, SoundCloud\n'
printf 'V2: import TXT/CSV/JSON/M3U/M3U8 + résolution manuelle + exports JSON/CSV/M3U8 + historique\n'

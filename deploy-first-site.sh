#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

chmod +x setup-worker.sh deploy.sh

# Refresh the private media worker so yt-dlp, gallery-dl, Chromium cookies and Deno
# are the same engines used by the current PANDA DL build.
./setup-worker.sh

# Reuse the original Cloud Run service name. Cloud Run keeps the existing service
# URL, including https://panda-download-579092510171.europe-west1.run.app/
PANDA_SERVICE=panda-download ./deploy.sh

URL="$(gcloud run services describe panda-download --region "${PANDA_REGION:-europe-west1}" --format='value(status.url)')"
echo
echo "===================================="
echo "PANDA DOWNLOAD · FULL SOCIAL ENGINE"
echo "$URL"
echo "===================================="
curl -fsS "$URL/health" || true
echo

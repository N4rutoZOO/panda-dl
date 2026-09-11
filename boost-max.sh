#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
TARGET_MACHINE="${PANDA_BOOST_MACHINE:-e2-standard-4}"

cd "$(dirname "$0")"
gcloud config set project "$PROJECT_ID" >/dev/null

CURRENT="$(gcloud compute instances describe "$INSTANCE" --zone "$ZONE" --format='value(machineType.basename())')"
if [[ "$CURRENT" != "$TARGET_MACHINE" ]]; then
  echo "Upgrade VM: $CURRENT -> $TARGET_MACHINE"
  gcloud compute instances stop "$INSTANCE" --zone "$ZONE" --quiet
  gcloud compute instances set-machine-type "$INSTANCE" --zone "$ZONE" --machine-type "$TARGET_MACHINE" --quiet
  gcloud compute instances start "$INSTANCE" --zone "$ZONE" --quiet
  echo "Attente du redémarrage..."
  sleep 12
else
  echo "VM déjà en $TARGET_MACHINE"
fi

# MAX useful profile: 2 simultaneous heavy jobs, 4 yt-dlp fragments per job,
# 4 vCPU / 16 GiB Cloud Run, warm instance to remove cold starts.
PANDA_RUN_CPU=4 \
PANDA_RUN_MEMORY=16Gi \
PANDA_RUN_CONCURRENCY=8 \
PANDA_MIN_INSTANCES=1 \
PANDA_JOB_WORKERS=2 \
PANDA_WORKER_JOBS=2 \
PANDA_WORKER_FRAGMENTS=4 \
./deploy-first-site.sh

echo
echo "===================================="
echo "PANDA DL MAX BOOST ACTIVE"
echo "VM: $TARGET_MACHINE"
echo "Cloud Run: 4 CPU / 16 GiB / warm"
echo "Worker: 2 jobs / 4 fragments"
echo "===================================="

#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_USER="${PANDA_WORKER_USER:-gbeerus489}"
SECRET="${PANDA_WORKER_SECRET:-panda-dl-worker-token}"
PORT="${PANDA_WORKER_PORT:-8865}"

cd "$(dirname "$0")"
python3 -m py_compile worker_server.py

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable compute.googleapis.com secretmanager.googleapis.com >/dev/null

if ! gcloud compute instances describe "$INSTANCE" --zone "$ZONE" >/dev/null 2>&1; then
  echo "ERREUR: VM $INSTANCE introuvable dans $ZONE"
  exit 1
fi

TOKEN="$(openssl rand -hex 32)"
if gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  printf '%s' "$TOKEN" | gcloud secrets versions add "$SECRET" --data-file=- >/dev/null
else
  printf '%s' "$TOKEN" | gcloud secrets create "$SECRET" --replication-policy=automatic --data-file=- >/dev/null
fi

ENV_FILE="$(mktemp)"
SERVICE_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE" "$SERVICE_FILE"' EXIT

cat > "$ENV_FILE" <<EOF
PANDA_WORKER_TOKEN=$TOKEN
PANDA_CHROME_PROFILE=/home/$WORKER_USER/chrome-profile
PANDA_WORKER_PORT=$PORT
PANDA_PLAYLIST_LIMIT=100
PANDA_WORKER_JOB_TTL=3600
PANDA_WORKER_JOBS=1
PANDA_WORKER_FRAGMENTS=2
PANDA_YTDLP_BIN=/opt/panda-dl-worker/venv/bin/yt-dlp
PATH=/opt/panda-dl-worker/venv/bin:/home/$WORKER_USER/.deno/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=PANDA DL yt-dlp worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$WORKER_USER
Group=$WORKER_USER
WorkingDirectory=/opt/panda-dl-worker
EnvironmentFile=/etc/panda-dl-worker.env
ExecStart=/opt/panda-dl-worker/venv/bin/python -m uvicorn worker_server:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF

echo "Installation PANDA DL worker..."
gcloud compute scp worker_server.py "$INSTANCE:/tmp/worker_server.py" --zone "$ZONE" >/dev/null
gcloud compute scp "$ENV_FILE" "$INSTANCE:/tmp/panda-dl-worker.env" --zone "$ZONE" >/dev/null
gcloud compute scp "$SERVICE_FILE" "$INSTANCE:/tmp/panda-dl-worker.service" --zone "$ZONE" >/dev/null

gcloud compute ssh "$INSTANCE" --zone "$ZONE" --command="
set -e
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv curl ca-certificates ffmpeg chromium unzip >/dev/null
mkdir -p /home/$WORKER_USER/chrome-profile /home/$WORKER_USER/Downloads
if [[ ! -x /home/$WORKER_USER/.deno/bin/deno ]]; then
  DENO_INSTALL=/home/$WORKER_USER/.deno curl -fsSL https://deno.land/install.sh | sh >/dev/null
fi
sudo mkdir -p /opt/panda-dl-worker
sudo mv /tmp/worker_server.py /opt/panda-dl-worker/worker_server.py
sudo chown -R $WORKER_USER:$WORKER_USER /opt/panda-dl-worker
if [[ ! -x /opt/panda-dl-worker/venv/bin/python ]]; then
  python3 -m venv /opt/panda-dl-worker/venv
fi
/opt/panda-dl-worker/venv/bin/pip install -q --upgrade pip
/opt/panda-dl-worker/venv/bin/pip install -q 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.32,<1' 'yt-dlp[default]>=2026.07.04,<2027'
sudo mv /tmp/panda-dl-worker.env /etc/panda-dl-worker.env
sudo mv /tmp/panda-dl-worker.service /etc/systemd/system/panda-dl-worker.service
sudo chown root:root /etc/panda-dl-worker.env /etc/systemd/system/panda-dl-worker.service
sudo chmod 600 /etc/panda-dl-worker.env
sudo systemctl daemon-reload
sudo systemctl enable --now panda-dl-worker
sleep 4
curl -fsS http://127.0.0.1:$PORT/health
"

SUBNET_RANGE="$(gcloud compute networks subnets describe default --region "$REGION" --format='value(ipCidrRange)')"
if gcloud compute firewall-rules describe panda-dl-worker-internal >/dev/null 2>&1; then
  gcloud compute firewall-rules update panda-dl-worker-internal --allow="tcp:${PORT}" --source-ranges="$SUBNET_RANGE" --target-tags=panda-youtube-worker --quiet >/dev/null
else
  gcloud compute firewall-rules create panda-dl-worker-internal --network=default --direction=INGRESS --action=ALLOW --rules="tcp:${PORT}" --source-ranges="$SUBNET_RANGE" --target-tags=panda-youtube-worker --quiet >/dev/null
fi

IP="$(gcloud compute instances describe "$INSTANCE" --zone "$ZONE" --format='value(networkInterfaces[0].networkIP)')"
echo
echo "===================================="
echo "PANDA DL WORKER READY"
echo "VM: $INSTANCE"
echo "Internal: http://$IP:$PORT"
echo "Secret: $SECRET"
echo "Profile: /home/$WORKER_USER/chrome-profile"
echo "===================================="

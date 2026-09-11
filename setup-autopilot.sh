#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
ZONE="${PANDA_AUTOPILOT_ZONE:-europe-west1-b}"
VM="${PANDA_AUTOPILOT_VM:-panda-autopilot}"
MACHINE="${PANDA_AUTOPILOT_MACHINE:-e2-medium}"
DISK_GB="${PANDA_AUTOPILOT_DISK_GB:-25}"
SA_NAME="${PANDA_AUTOPILOT_SA:-panda-autopilot}"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO="${PANDA_AUTOPILOT_REPO:-N4rutoZOO/panda-dl}"

export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"
gcloud config set project "$PROJECT_ID" >/dev/null

gcloud services enable \
  compute.googleapis.com \
  iap.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  logging.googleapis.com \
  aiplatform.googleapis.com \
  iam.googleapis.com >/dev/null

if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --display-name="PANDA Autopilot" >/dev/null
fi

for role in \
  roles/aiplatform.user \
  roles/run.admin \
  roles/logging.viewer \
  roles/compute.instanceAdmin.v1 \
  roles/compute.osAdminLogin \
  roles/iap.tunnelResourceAccessor \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer \
  roles/secretmanager.secretAccessor \
  roles/iam.serviceAccountUser \
  roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="$role" --quiet >/dev/null
 done

# IAP SSH only for the dedicated agent tag.
if ! gcloud compute firewall-rules describe panda-autopilot-iap-ssh >/dev/null 2>&1; then
  gcloud compute firewall-rules create panda-autopilot-iap-ssh \
    --network=default \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags=panda-autopilot \
    --quiet >/dev/null
fi

if ! gcloud compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  echo "Création VM privée PANDA AUTOPILOT (${MACHINE}, ${DISK_GB}GB)..."
  gcloud compute instances create "$VM" \
    --zone "$ZONE" \
    --machine-type "$MACHINE" \
    --boot-disk-size "${DISK_GB}GB" \
    --image-family debian-12 \
    --image-project debian-cloud \
    --network default \
    --subnet default \
    --no-address \
    --tags panda-autopilot \
    --service-account "$SA" \
    --scopes cloud-platform \
    --quiet >/dev/null
else
  status="$(gcloud compute instances describe "$VM" --zone "$ZONE" --format='value(status)')"
  if [[ "$status" != "RUNNING" ]]; then
    gcloud compute instances start "$VM" --zone "$ZONE" --quiet >/dev/null
  fi
fi

# Wait for IAP/SSH.
echo "Attente SSH IAP..."
for i in $(seq 1 30); do
  if gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --quiet --command='echo ok' >/dev/null 2>&1; then
    break
  fi
  [[ "$i" == "30" ]] && { echo "SSH IAP indisponible" >&2; exit 1; }
  sleep 5
done

# A private VM needs outbound internet. Create Cloud NAT only if needed.
if ! gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --quiet \
  --command='curl -fsSI --max-time 8 https://github.com >/dev/null' >/dev/null 2>&1; then
  echo "Pas d'egress Internet: création Cloud NAT..."
  ROUTER="panda-autopilot-router"
  NAT="panda-autopilot-nat"
  gcloud compute routers describe "$ROUTER" --region "$REGION" >/dev/null 2>&1 || \
    gcloud compute routers create "$ROUTER" --network default --region "$REGION" --quiet >/dev/null
  gcloud compute routers nats describe "$NAT" --router "$ROUTER" --region "$REGION" >/dev/null 2>&1 || \
    gcloud compute routers nats create "$NAT" --router "$ROUTER" --region "$REGION" \
      --nat-all-subnet-ip-ranges --auto-allocate-nat-external-ips --quiet >/dev/null
  sleep 15
fi

# Bootstrap agent VM.
gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap --quiet --command='sudo bash -s' <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git gnupg jq python3 python3-pip python3-venv docker.io gh
systemctl enable --now docker

# Node.js 22 for current Gemini CLI.
if ! command -v node >/dev/null || [ "\$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
npm install -g @google/gemini-cli@latest

id panda-autopilot >/dev/null 2>&1 || useradd --create-home --shell /bin/bash panda-autopilot
usermod -aG docker panda-autopilot
mkdir -p /opt/panda-autopilot /var/lib/panda-autopilot
chown -R panda-autopilot:panda-autopilot /opt/panda-autopilot /var/lib/panda-autopilot

if [ ! -d /opt/panda-autopilot/repo/.git ]; then
  sudo -u panda-autopilot git clone https://github.com/${REPO}.git /opt/panda-autopilot/repo
else
  sudo -u panda-autopilot git -C /opt/panda-autopilot/repo fetch --prune origin
  sudo -u panda-autopilot git -C /opt/panda-autopilot/repo reset --hard origin/main
fi
chmod +x /opt/panda-autopilot/repo/autopilot.sh /opt/panda-autopilot/repo/autopilot-deploy.sh

cat >/etc/panda-autopilot.env <<'ENV'
PANDA_GCP_PROJECT=${PROJECT_ID}
PANDA_REGION=${REGION}
PANDA_PROD_SERVICE=panda-download
PANDA_STAGING_SERVICE=panda-download-staging
PANDA_PROD_URL=https://panda-download-579092510171.europe-west1.run.app
PANDA_WORKER_INSTANCE=panda-youtube-worker
PANDA_WORKER_ZONE=europe-west1-b
PANDA_AUTOPILOT_REPO=${REPO}
PANDA_AUTOPILOT_REPO_DIR=/opt/panda-autopilot/repo
PANDA_AUTOPILOT_STATE_DIR=/var/lib/panda-autopilot
PANDA_AUTOPILOT_CODE_FIX=1
PANDA_AUTOPILOT_PUSH=0
PANDA_AUTOPILOT_AUTO_DEPLOY=0
PANDA_AUTOPILOT_AUTO_MERGE=0
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
GOOGLE_CLOUD_LOCATION=global
ENV
chmod 640 /etc/panda-autopilot.env
chown root:panda-autopilot /etc/panda-autopilot.env

cat >/etc/systemd/system/panda-autopilot.service <<'UNIT'
[Unit]
Description=PANDA DOWNLOAD autonomous maintenance cycle
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=panda-autopilot
Group=panda-autopilot
SupplementaryGroups=docker
WorkingDirectory=/opt/panda-autopilot/repo
EnvironmentFile=/etc/panda-autopilot.env
ExecStart=/opt/panda-autopilot/repo/autopilot.sh
Nice=10
CPUWeight=40
IOSchedulingClass=best-effort
IOSchedulingPriority=6
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/panda-autopilot /var/lib/panda-autopilot /tmp
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/panda-autopilot.timer <<'TIMER'
[Unit]
Description=Run PANDA AUTOPILOT every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=20
Persistent=true
Unit=panda-autopilot.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now panda-autopilot.timer
systemctl start panda-autopilot.service || true
REMOTE

echo
echo "=============================================="
echo "PANDA AUTOPILOT V1 installé"
echo "VM: $VM ($ZONE)"
echo "Cycle: toutes les 5 minutes"
echo "Gemini: Vertex AI / Compute ADC"
echo "Self-heal: ACTIF"
echo "Auto code fix: ACTIF"
echo "Push GitHub: OFF"
echo "Auto deploy prod: OFF"
echo "Design V5: VERROUILLE"
echo "=============================================="
echo
echo "Logs:"
echo "gcloud compute ssh $VM --zone $ZONE --tunnel-through-iap --command='sudo journalctl -u panda-autopilot -n 100 --no-pager'"
echo
echo "Pour l'autonomie totale plus tard: authentifie GitHub sur la VM puis active PUSH/AUTO_DEPLOY/AUTO_MERGE dans /etc/panda-autopilot.env."

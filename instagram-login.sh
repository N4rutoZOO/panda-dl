#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_USER="${PANDA_WORKER_USER:-gbeerus489}"
PROFILE="/home/${WORKER_USER}/chrome-profile"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "Préparation du navigateur privé sur $INSTANCE..."
gcloud compute ssh "$INSTANCE" --zone "$ZONE" --command="
set -e
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y xvfb fluxbox x11vnc novnc websockify chromium >/dev/null
mkdir -p '$PROFILE'

# Keep every remote-desktop service bound to localhost only.
pkill -f 'websockify.*6080' >/dev/null 2>&1 || true
pkill -f 'x11vnc.*5900' >/dev/null 2>&1 || true

pgrep -f 'Xvfb :99' >/dev/null || nohup Xvfb :99 -screen 0 1440x900x24 >/tmp/panda-xvfb.log 2>&1 &
sleep 1
DISPLAY=:99 pgrep -f fluxbox >/dev/null || DISPLAY=:99 nohup fluxbox >/tmp/panda-fluxbox.log 2>&1 &
sleep 1

if ! pgrep -f "chromium.*${PROFILE}" >/dev/null; then
  DISPLAY=:99 nohup chromium \
    --user-data-dir='$PROFILE' \
    --no-first-run \
    --disable-dev-shm-usage \
    'https://www.instagram.com/accounts/login/' \
    >/tmp/panda-chromium.log 2>&1 &
fi

nohup x11vnc -display :99 -localhost -forever -shared -rfbport 5900 -nopw >/tmp/panda-x11vnc.log 2>&1 &
nohup websockify --web=/usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 >/tmp/panda-novnc.log 2>&1 &
sleep 2
ss -ltn | grep -E '127.0.0.1:(5900|6080)'
"

# Kill an older local tunnel if one already owns 6080, then recreate it.
pkill -f "ssh.*127.0.0.1:6080:127.0.0.1:6080" >/dev/null 2>&1 || true
gcloud compute ssh "$INSTANCE" --zone "$ZONE" -- -4 -fN \
  -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:6080:127.0.0.1:6080

echo
echo "===================================="
echo "INSTAGRAM LOGIN READY"
echo "Dans Cloud Shell: Web Preview -> Change port -> 6080"
echo "Connecte TON compte Instagram dans Chromium."
echo "Pour un compte privé, ce compte doit déjà suivre le profil concerné."
echo "Ne ferme pas la session Instagram; les cookies restent dans $PROFILE."
echo "===================================="

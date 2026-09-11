#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PANDA_GCP_PROJECT:-project-017b13a1-e57e-4723-a2b}"
REGION="${PANDA_REGION:-europe-west1}"
PROD_SERVICE="${PANDA_PROD_SERVICE:-panda-download}"
PROD_URL="${PANDA_PROD_URL:-https://panda-download-579092510171.europe-west1.run.app}"
WORKER_INSTANCE="${PANDA_WORKER_INSTANCE:-panda-youtube-worker}"
WORKER_ZONE="${PANDA_WORKER_ZONE:-europe-west1-b}"
REPO="${PANDA_AUTOPILOT_REPO:-N4rutoZOO/panda-dl}"
REPO_DIR="${PANDA_AUTOPILOT_REPO_DIR:-/opt/panda-autopilot/repo}"
STATE_DIR="${PANDA_AUTOPILOT_STATE_DIR:-/var/lib/panda-autopilot}"
AUTO_CODE_FIX="${PANDA_AUTOPILOT_CODE_FIX:-1}"
PUSH_CHANGES="${PANDA_AUTOPILOT_PUSH:-0}"
AUTO_DEPLOY="${PANDA_AUTOPILOT_AUTO_DEPLOY:-0}"
AUTO_MERGE="${PANDA_AUTOPILOT_AUTO_MERGE:-0}"

mkdir -p "$STATE_DIR"
exec 9>"${STATE_DIR}/autopilot.lock"
flock -n 9 || exit 0

log(){ printf '%s %s\n' "$(date -Is)" "$*" | tee -a "${STATE_DIR}/autopilot.log"; }
health_json(){ curl -fsS --max-time 20 "${PROD_URL}/health" 2>/dev/null || true; }
health_ok(){
  local h="$(health_json)"
  [[ -n "$h" ]] || return 1
  python3 - "$h" <<'PY'
import json,sys
try:d=json.loads(sys.argv[1])
except Exception:raise SystemExit(1)
if d.get('status')!='ok':raise SystemExit(1)
if d.get('worker_configured') is not True:raise SystemExit(1)
if d.get('photo_download') is not True:raise SystemExit(1)
PY
}

self_heal(){
  log "SELF-HEAL: vérification worker"
  local status="$(gcloud compute instances describe "$WORKER_INSTANCE" --project "$PROJECT_ID" --zone "$WORKER_ZONE" --format='value(status)' 2>/dev/null || true)"
  if [[ "$status" != "RUNNING" ]]; then
    log "SELF-HEAL: démarrage VM worker (état=${status:-inconnu})"
    gcloud compute instances start "$WORKER_INSTANCE" --project "$PROJECT_ID" --zone "$WORKER_ZONE" --quiet || true
    sleep 12
  fi
  log "SELF-HEAL: restart panda-dl-worker"
  gcloud compute ssh "$WORKER_INSTANCE" --project "$PROJECT_ID" --zone "$WORKER_ZONE" --tunnel-through-iap --quiet \
    --command='sudo systemctl restart panda-dl-worker && sleep 2 && sudo systemctl is-active panda-dl-worker' >/dev/null 2>&1 || true
  sleep 8
}

collect_incident(){
  local out="$1"
  {
    echo "# PANDA AUTOPILOT INCIDENT"
    echo "date=$(date -Is)"
    echo "prod_url=$PROD_URL"
    echo; echo "## /health"; health_json
    echo; echo "## Cloud Run"
    gcloud run services describe "$PROD_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
      --format='yaml(status.conditions,status.latestReadyRevisionName,status.latestCreatedRevisionName)' 2>&1 || true
    echo; echo "## Cloud Run recent errors"
    gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=${PROD_SERVICE} AND severity>=ERROR" \
      --project "$PROJECT_ID" --freshness=45m --limit=120 --format='value(timestamp,severity,textPayload,jsonPayload.message)' 2>&1 || true
    echo; echo "## Worker VM"
    gcloud compute instances describe "$WORKER_INSTANCE" --project "$PROJECT_ID" --zone "$WORKER_ZONE" \
      --format='yaml(status,machineType,networkInterfaces[0].networkIP)' 2>&1 || true
    echo; echo "## Worker journal"
    gcloud compute ssh "$WORKER_INSTANCE" --project "$PROJECT_ID" --zone "$WORKER_ZONE" --tunnel-through-iap --quiet \
      --command='sudo journalctl -u panda-dl-worker --no-pager -n 160; echo; df -h / /tmp /opt 2>/dev/null || true' 2>&1 || true
  } | sed -E \
    -e 's/(Authorization:[[:space:]]*Bearer)[[:space:]]+[^[:space:]]+/\1 [REDACTED]/Ig' \
    -e 's/(AIza[0-9A-Za-z_-]{10,})/[REDACTED_GOOGLE_KEY]/g' \
    -e 's/(github_pat_[0-9A-Za-z_]+)/[REDACTED_GITHUB_TOKEN]/g' \
    -e 's/(ghp_[0-9A-Za-z]+)/[REDACTED_GITHUB_TOKEN]/g' >"$out"
}

ensure_repo(){
  [[ -d "$REPO_DIR/.git" ]] || { log "Repo agent manquant: $REPO_DIR"; return 1; }
  git -C "$REPO_DIR" worktree prune >/dev/null 2>&1 || true
  git -C "$REPO_DIR" fetch --prune origin
  git -C "$REPO_DIR" checkout main >/dev/null 2>&1 || true
  git -C "$REPO_DIR" reset --hard origin/main >/dev/null
  git -C "$REPO_DIR" clean -fd >/dev/null
}

validate_candidate(){
  local wt="$1" path
  mapfile -t changed < <(git -C "$wt" status --porcelain | sed -E 's/^.. //' | sed -E 's/.* -> //')
  ((${#changed[@]})) || { log "Gemini n'a produit aucun changement"; return 1; }
  for path in "${changed[@]}"; do
    case "$path" in
      app_full.py|app_max.py|worker_server.py|worker_server_resilient.py|setup-worker.sh|deploy-first-site.sh|requirements.txt|Dockerfile) ;;
      *) log "REJET: fichier non autorisé modifié: $path"; return 1;;
    esac
  done
  git -C "$wt" diff --check
  if git -C "$wt" diff | grep -Eq '(AIza[0-9A-Za-z_-]{20,}|github_pat_[0-9A-Za-z_]+|ghp_[0-9A-Za-z]+|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)'; then
    log "REJET: secret potentiel détecté dans le diff"; return 1
  fi
  while IFS= read -r f; do python3 -m py_compile "$f"; done < <(find "$wt" -maxdepth 1 -type f -name '*.py' | sort)
  while IFS= read -r f; do bash -n "$f"; done < <(find "$wt" -maxdepth 1 -type f -name '*.sh' | sort)
}

code_fix() (
  [[ "$AUTO_CODE_FIX" == "1" ]] || { log "AUTO_CODE_FIX=0"; return 1; }
  command -v gemini >/dev/null || { log "Gemini CLI absent"; return 1; }
  ensure_repo

  local ts="$(date +%Y%m%d-%H%M%S)"
  local branch="autopilot/fix-${ts}"
  local wt="/tmp/panda-autofix-${ts}"
  local incident="${wt}/.autopilot-incident.txt"
  local promote=""
  cleanup(){
    [[ -z "$promote" ]] || git -C "$REPO_DIR" worktree remove --force "$promote" >/dev/null 2>&1 || true
    git -C "$REPO_DIR" worktree remove --force "$wt" >/dev/null 2>&1 || true
    git -C "$REPO_DIR" worktree prune >/dev/null 2>&1 || true
  }
  trap cleanup EXIT

  git -C "$REPO_DIR" worktree add -b "$branch" "$wt" origin/main >/dev/null
  collect_incident "$incident"

  log "Gemini analyse l'incident sur $branch"
  (
    cd "$wt"
    unset DEBUG
    export GOOGLE_GENAI_USE_VERTEXAI="${GOOGLE_GENAI_USE_VERTEXAI:-true}"
    export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-$PROJECT_ID}"
    export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
    gemini --sandbox -y -p 'Lis AUTOPILOT.md puis .autopilot-incident.txt. Répare uniquement la cause racine. Respecte strictement les invariants et la whitelist. Ne touche jamais index.html. Correctif minimal seulement. Lance les vérifications utiles. Ne déploie rien.'
  ) >>"${STATE_DIR}/gemini-${ts}.log" 2>&1 || true

  rm -f "$incident"
  git -C "$wt" checkout -- AUTOPILOT.md 2>/dev/null || true
  validate_candidate "$wt" || { log "Candidat rejeté"; return 1; }

  git -C "$wt" config user.name "PANDA Autopilot"
  git -C "$wt" config user.email "autopilot@panda.local"
  git -C "$wt" add app_full.py app_max.py worker_server.py worker_server_resilient.py setup-worker.sh deploy-first-site.sh requirements.txt Dockerfile 2>/dev/null || true
  git -C "$wt" commit -m "autopilot: repair production incident ${ts}" >/dev/null
  log "Correctif validé localement: $branch"

  local pushed=0
  if [[ "$PUSH_CHANGES" == "1" ]]; then
    if git -C "$wt" push -u origin "$branch"; then
      pushed=1
      if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
        (cd "$wt" && gh pr create --repo "$REPO" --base main --head "$branch" --title "Autopilot: incident ${ts}" \
          --body "Correctif automatique PANDA AUTOPILOT. Tests locaux validés. Le design V5 est verrouillé." >/dev/null) || true
      fi
    fi
  fi

  log "Déploiement staging du candidat"
  chmod +x "$wt/autopilot-deploy.sh"
  PANDA_AUTOPILOT_AUTO_DEPLOY=0 "$wt/autopilot-deploy.sh" "$wt" || { log "Staging KO"; return 1; }

  [[ "$AUTO_DEPLOY" == "1" ]] || { log "Staging OK. AUTO_DEPLOY=0: pas de promotion prod."; return 0; }
  [[ "$pushed" == "1" && "$AUTO_MERGE" == "1" ]] || { log "Promotion bloquée: PUSH=1 et AUTO_MERGE=1 requis."; return 1; }
  command -v gh >/dev/null && gh auth status >/dev/null 2>&1 || { log "GitHub CLI non authentifié"; return 1; }

  log "Staging OK: merge PR"
  (cd "$wt" && gh pr merge "$branch" --repo "$REPO" --squash --delete-branch) || { log "Merge PR impossible"; return 1; }
  git -C "$REPO_DIR" fetch origin main
  promote="/tmp/panda-promote-${ts}"
  git -C "$REPO_DIR" worktree add --detach "$promote" origin/main >/dev/null
  chmod +x "$promote/autopilot-deploy.sh"
  PANDA_AUTOPILOT_AUTO_DEPLOY=1 "$promote/autopilot-deploy.sh" "$promote"
  log "Promotion production terminée"
)

main(){
  gcloud config set project "$PROJECT_ID" >/dev/null 2>&1 || true
  if health_ok; then log "HEALTH OK"; exit 0; fi
  log "INCIDENT détecté sur $PROD_URL"
  self_heal
  if health_ok; then log "SELF-HEAL réussi"; exit 0; fi
  log "SELF-HEAL insuffisant, escalade agent IA"
  code_fix || true
  if health_ok; then log "HEALTH OK après intervention"; else log "HEALTH toujours KO; intervention humaine recommandée"; fi
}

main "$@"

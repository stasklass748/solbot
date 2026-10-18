#!/usr/bin/env bash
# Start a 48-hour prospective run only after the operator chooses a credit cap.
set -Eeuo pipefail

ROOT=/opt/meme-narrative-collector/meme-narrative-collector
ENV_FILE="$ROOT/.env"
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
for unit in meme-x-first.timer meme-x-followup.timer meme-source-metadata.timer; do
  [[ -f /etc/systemd/system/$unit ]] || { echo "Missing $unit; run install_research_v2.sh first" >&2; exit 1; }
done
available_kb=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
[[ $available_kb -ge 3145728 ]] || { echo "Less than 3 GiB free; refusing new run" >&2; exit 1; }
[[ ${PILOT_X_CREDIT_CAP:-} =~ ^[0-9]+$ ]] || {
  echo "Set PILOT_X_CREDIT_CAP to a numeric cap after checking your provider balance" >&2; exit 1;
}
[[ $PILOT_X_CREDIT_CAP -ge 1000 && $PILOT_X_CREDIT_CAP -le 650000 ]] || {
  echo "PILOT_X_CREDIT_CAP must be between 1000 and 650000" >&2; exit 1;
}

old_end=$(sed -n 's/^X_FIRST_END_AT=//p' "$ENV_FILE" | tail -n 1)
old_id=$(sed -n 's/^X_FIRST_RUN_ID=//p' "$ENV_FILE" | tail -n 1)
if [[ -n $old_end && -n $old_id ]] && old_epoch=$(date -u -d "$old_end" +%s 2>/dev/null); then
  if (( old_epoch > $(date -u +%s) )); then
    echo "Existing X-first run $old_id has not ended ($old_end). Not resetting its budget." >&2
    exit 1
  fi
fi

set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

start_time=$(date -u +%Y%m%dT%H%M%SZ)
start_iso=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
end_iso=$(date -u -d '+48 hours' +%Y-%m-%dT%H:%M:%SZ)

systemctl stop meme-x-first.timer meme-x-followup.timer meme-source-metadata.timer 2>/dev/null || true
set_env X_FIRST_RUN_ID "$start_time"
set_env X_FIRST_END_AT "$end_iso"
set_env X_FIRST_CREDIT_CAP "$PILOT_X_CREDIT_CAP"
set_env X_FIRST_FOLLOWUP_SAMPLE_PERCENT 10
set_env X_FIRST_ENABLED true
set_env SOURCE_METADATA_SINCE "$start_iso"
set_env SOURCE_METADATA_END_AT "$end_iso"
set_env SOURCE_METADATA_MAX_JOBS_PER_RUN 10
set_env SOCIAL_COLLECT_MIGRATION_BASELINE false
set_env SOCIAL_COLLECT_CANDIDATES false
chown collector:collector "$ENV_FILE"
chmod 600 "$ENV_FILE"
systemctl enable --now meme-x-first.timer meme-x-followup.timer meme-source-metadata.timer
echo "48-hour pilot started; end: $end_iso; X cap: $PILOT_X_CREDIT_CAP estimated credits."
echo "This is an internal cap, not a live balance check."
systemctl is-active meme-x-first.timer meme-x-followup.timer meme-source-metadata.timer

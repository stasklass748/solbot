#!/usr/bin/env bash
# Explicitly start an X -> AI -> Telegram run. Zero credit cap means unlimited.
set -Eeuo pipefail

ROOT=/opt/meme-narrative-collector/meme-narrative-collector
ENV_FILE="$ROOT/.env"
PYTHON="$ROOT/.venv/bin/python"

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -f "$ENV_FILE" && -x "$PYTHON" ]] || { echo "Collector or .env missing" >&2; exit 1; }
[[ -f "$ROOT/idea_x_queries.json" ]] || { echo "idea_x_queries.json missing" >&2; exit 1; }
for unit in meme-x-first.timer meme-idea-analyzer.timer meme-idea-telegram.service; do
  [[ -f /etc/systemd/system/$unit ]] || { echo "Missing $unit" >&2; exit 1; }
done
IDEA_X_CREDIT_CAP=${IDEA_X_CREDIT_CAP:-0}
[[ $IDEA_X_CREDIT_CAP =~ ^[0-9]+$ ]] || {
  echo "IDEA_X_CREDIT_CAP must be numeric; 0 disables the internal run cap" >&2; exit 1;
}
[[ $IDEA_X_CREDIT_CAP -eq 0 || ( $IDEA_X_CREDIT_CAP -ge 1000 && $IDEA_X_CREDIT_CAP -le 200000 ) ]] || {
  echo "IDEA_X_CREDIT_CAP must be 0 or between 1000 and 200000" >&2; exit 1;
}
available_kb=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
[[ $available_kb -ge 2097152 ]] || { echo "Less than 2 GiB free; refusing new run" >&2; exit 1; }

sudo -u collector "$PYTHON" -c \
  'import os,sys; from pathlib import Path; from narrative_collector.config import load_dotenv; from narrative_collector.telegram_ui import parse_chat_ids; load_dotenv(Path(sys.argv[1])); keys=("TWITTERAPI_IO_KEY","OPENAI_API_KEY","TELEGRAM_BOT_TOKEN"); missing=[key for key in keys if not os.getenv(key)]; raw=os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", ""); print("Missing required keys: "+", ".join(missing) if missing else "API keys: present (values hidden)"); chats=parse_chat_ids(raw); no_reviewers=any(chat.startswith("-") for chat in chats) and not os.getenv("TELEGRAM_ALLOWED_USER_IDS"); print("Chat IDs configured: "+str(len(chats))); print("Group chat requires TELEGRAM_ALLOWED_USER_IDS" if no_reviewers else "Reviewer access: configured"); sys.exit(bool(missing or no_reviewers))' \
  "$ENV_FILE"

old_id=$(sed -n 's/^X_FIRST_RUN_ID=//p' "$ENV_FILE" | tail -n 1)
if [[ -n $old_id ]]; then
  echo "Existing X-first run $old_id keeps its credit budget. Refusing to reset it." >&2
  exit 1
fi

set_env() {
  local key=$1 value=$2
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

run_id=$(date -u +%Y%m%dT%H%M%SZ)
systemctl disable --now meme-idea-telegram.timer 2>/dev/null || true
systemctl stop meme-x-first.timer meme-idea-analyzer.timer meme-idea-telegram.service 2>/dev/null || true
set_env X_FIRST_RUN_ID "$run_id"
set_env X_FIRST_END_AT ""
set_env X_FIRST_CREDIT_CAP "$IDEA_X_CREDIT_CAP"
set_env X_FIRST_ENABLED true
set_env X_FIRST_FOLLOWUP_SAMPLE_PERCENT 0
set_env X_FIRST_QUERIES_PATH idea_x_queries.json
set_env AI_BOT_AUTO_ENABLED true
chown collector:collector "$ENV_FILE"
chmod 600 "$ENV_FILE"
systemctl enable --now meme-x-first.timer meme-idea-analyzer.timer meme-idea-telegram.service
echo "Live idea run started without a time limit; X internal cap: $IDEA_X_CREDIT_CAP (0=unlimited)."
echo "AI caps come from .env; this does not check provider balances or OpenAI project spend limit."
systemctl is-active meme-x-first.timer meme-idea-analyzer.timer meme-idea-telegram.service

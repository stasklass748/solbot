#!/usr/bin/env bash
# Explicit single 24h pilot. Budgets are persisted and cannot be reset by rerunning.
set -Eeuo pipefail
APP=/opt/meme-narrative-collector/meme-narrative-collector
PYTHON="$APP/.venv/bin/python"
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -x "$PYTHON" && -f "$APP/.env" ]] || { echo "Collector or .env missing" >&2; exit 1; }
[[ -f /etc/systemd/system/meme-migration-learning.timer ]] || {
  echo "Run install_ai_learning.sh first" >&2; exit 1;
}
sudo -u collector "$PYTHON" -c \
  'import os,sys; from pathlib import Path; from narrative_collector.config import load_dotenv; load_dotenv(Path(sys.argv[1])); missing=[k for k in ("TWITTERAPI_IO_KEY","OPENAI_API_KEY") if not os.getenv(k)]; print("API keys present (hidden)" if not missing else "Missing "+", ".join(missing)); sys.exit(bool(missing))' \
  "$APP/.env"
sudo -u collector "$PYTHON" -m narrative_collector.migration_learning --env-dir "$APP" --start
systemctl enable --now meme-migration-learning.timer
echo "24h research pilot started: maximum 10 X requests and 50 AI calls."
echo "Results are research drafts; live filters are not modified."

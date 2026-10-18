#!/usr/bin/env bash
# Explicit keyword run; zero internal X credit cap means unlimited.
set -Eeuo pipefail
APP=/opt/meme-narrative-collector/meme-narrative-collector
PYTHON="$APP/.venv/bin/python"
MODE=${1:---start}
[[ $# -le 1 && ( $MODE == --start || $MODE == --renew ) ]] || {
  echo "Usage: start_x_keywords.sh [--renew]" >&2; exit 1;
}
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
[[ -x "$PYTHON" && -f "$APP/.env" && -f "$APP/x_keyword_terms.txt" ]] || {
  echo "Install or .env missing" >&2; exit 1;
}
sudo -u collector "$PYTHON" -c \
  'import os,sys; from pathlib import Path; from narrative_collector.config import load_dotenv; load_dotenv(Path(sys.argv[1])); print("X key present (hidden)" if os.getenv("TWITTERAPI_IO_KEY") else "TWITTERAPI_IO_KEY missing"); sys.exit(not bool(os.getenv("TWITTERAPI_IO_KEY")))' \
  "$APP/.env"
sudo -u collector "$PYTHON" -m narrative_collector.x_keyword_search --env-dir "$APP" "$MODE"
systemctl enable --now meme-x-keywords.timer
echo "Keyword run started without a time limit or internal X credit cap."
echo "Provider limits and charges still apply; estimated usage is recorded."
if ! systemctl is-active --quiet meme-idea-analyzer.timer; then
  echo "Warning: idea AI timer is inactive; posts will be stored but no new AI cards sent."
fi

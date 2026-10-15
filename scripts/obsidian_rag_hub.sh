#!/usr/bin/env bash
# Wrapper for obsidian_rag_hub.py. Sources an optional env file so cron/launchd
# runs can pick up API keys, then runs the hub with your current interpreter.
set -euo pipefail

ENV_FILE="${RAG_HUB_ENV_FILE:-$HOME/.config/osba-wiki/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
exec python3 "$(cd "$(dirname "$0")" && pwd)/obsidian_rag_hub.py" "$@"

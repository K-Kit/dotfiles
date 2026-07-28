#!/usr/bin/env bash
# EX-9 tripwire wrapper: run the detector daily, notify on findings only.
#
# vault_sync.py tripwire exits 2 when it finds something and 0 when clean, so a
# clean run stays silent by design -- a daily "nothing to report" message trains
# you to ignore the channel.
#
# Telegram delivery is optional and off until a token exists. Create
# ~/.claude/channels/telegram/tripwire.env with:
#     TELEGRAM_BOT_TOKEN=...
#     TELEGRAM_CHAT_ID=...
# Findings are always written to the log regardless, so detection never depends
# on the notification channel being configured.

set -uo pipefail

TOOL="${VAULT_SYNC_TOOL:-$HOME/code/dotfiles/scripts/vault/vault_sync.py}"
LOG_DIR="${VAULT_SYNC_LOG_DIR:-$HOME/.local/state/vault-sync}"
ENV_FILE="${VAULT_SYNC_TELEGRAM_ENV:-$HOME/.claude/channels/telegram/tripwire.env}"

mkdir -p "$LOG_DIR"
stamp=$(date -u +%Y-%m-%d_%H-%M-%S)
report="$LOG_DIR/tripwire-$stamp.txt"

uv run "$TOOL" tripwire "$@" >"$report" 2>&1
status=$?

ln -sfn "$report" "$LOG_DIR/latest.txt"

if [ "$status" -eq 0 ]; then
  exit 0
fi

if [ "$status" -ne 2 ]; then
  echo "vault-sync tripwire errored (exit $status); see $report" >&2
  exit "$status"
fi

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090  # runtime path, not resolvable at lint time
  . "$ENV_FILE"
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  # Telegram caps messages at 4096 chars; the summary lines are what matter.
  body=$(grep -E "REGENERABLE|OVER THE|regenerable dir" "$report" | head -20)
  curl -sS --max-time 30 \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=vault sync tripwire found something:

${body}

full report: ${report}" >/dev/null \
    || echo "tripwire: telegram delivery failed; report at $report" >&2
else
  echo "tripwire: findings recorded at $report (no telegram token configured)" >&2
fi

exit 0

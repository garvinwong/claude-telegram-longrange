#!/usr/bin/env bash
# install.sh — install the daemon as a systemd --user service (idempotent).
#
# Prerequisites:
#   - Python 3.10+ with `requests` installed
#   - Claude Code CLI logged in (`claude` on PATH)
#   - bot token configured: either export TGLR_BOT_TOKEN / TGLR_CHAT_ID,
#     or create ~/.claude-telegram-longrange/config.json (see config.example.json)
#
# What it does:
#   1. deleteWebhook (stale webhooks silently swallow long-poll updates)
#   2. render + install the systemd --user unit pointing at this checkout
#   3. NOT enable/start it (that is a deliberate step you run yourself)
#
# Usage: bash launch/install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/src"
WORKDIR="${TGLR_WORKDIR:-$HOME}"                 # cwd for the claude subprocess
STATE_DIR="${TGLR_STATE_DIR:-$HOME/.claude-telegram-longrange}"
UNIT_SRC="$REPO/launch/tg-longrange.service"
UNIT_DST="$HOME/.config/systemd/user/claude-telegram-longrange.service"

mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"

# 1. deleteWebhook (best-effort; needs a token available)
REPO="$REPO" python3 - <<'PY' || true
import sys, os
sys.path.insert(0, os.path.join(os.environ["REPO"], "src"))
import config, urllib.request
if not config.BOT_TOKEN:
    print("   (no token yet — skip deleteWebhook; set TGLR_BOT_TOKEN or config.json)")
    sys.exit(0)
try:
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{config.BOT_TOKEN}/deleteWebhook", timeout=10)
    print("1) deleteWebhook done")
except Exception as e:
    print("   deleteWebhook failed (non-fatal):", config.redact(repr(e)))
PY

# 2. render + install the unit (substitute install paths)
mkdir -p "$(dirname "$UNIT_DST")"
sed -e "s#@@WORKDIR@@#$WORKDIR#g" -e "s#@@SRC@@#$SRC#g" "$UNIT_SRC" > "$UNIT_DST"
systemctl --user daemon-reload || true
echo "2) installed $UNIT_DST"

cat <<EOF

Install complete. To run:
  systemctl --user enable --now claude-telegram-longrange
  systemctl --user status claude-telegram-longrange      # expect: active, 3 threads
Logs: /tmp/claude-telegram-longrange.log

Reminder: only ONE poller may hold the bot's long-poll. Before starting,
make sure no other bot/daemon uses the same token (Telegram returns 409 on conflict).
EOF

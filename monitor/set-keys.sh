#!/usr/bin/env bash
# Prompts for the monitor's secrets (hidden input) and writes them into monitor/.env.
# Safe to re-run any time: it starts from .env.example and overwrites .env.
set -u
cd "$(dirname "$0")"
ask() {
  local v
  while true; do
    read -rsp "$1 (hidden, paste then Enter): " v; echo
    v="${v//[[:space:]]/}"
    if [ -n "$v" ]; then echo "  received ${#v} characters"; printf -v "$2" '%s' "$v"; return; fi
    echo "  nothing received, try again (Ctrl+C to quit)"
  done
}
ask "Alpaca API key ID" K
ask "Alpaca secret key" S
ask "Telegram bot token" T
C=$(curl -s "https://api.telegram.org/bot$T/getUpdates" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ids = [u["message"]["chat"]["id"] for u in d.get("result", []) if "message" in u]
print(ids[-1] if ids else "")' 2>/dev/null)
K="$K" S="$S" T="$T" C="$C" python3 - <<'PY'
import os
vals = {"ALPACA_KEY_ID": os.environ["K"], "ALPACA_SECRET_KEY": os.environ["S"],
        "TELEGRAM_BOT_TOKEN": os.environ["T"], "TELEGRAM_CHAT_ID": os.environ["C"]}
out = []
for line in open(".env.example"):
    key = line.split("=", 1)[0]
    out.append(f"{key}={vals[key]}\n" if key in vals and not line.startswith("#") else line)
open(".env", "w").write("".join(out))
PY
unset K S T
chmod 600 .env
id deskmon >/dev/null 2>&1 && chown deskmon:deskmon .env
if [ -n "$C" ]; then echo "SAVED. Telegram chat found."; else
  echo "SAVED, but no Telegram chat was found. Check the token, send your bot a message, then run: bash set-keys.sh"; fi
unset C

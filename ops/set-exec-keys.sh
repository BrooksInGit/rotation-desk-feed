#!/usr/bin/env bash
# Prompts (hidden input) for the Alpaca PAPER account the desk will place its own paper orders in, checks the keys
# against Alpaca's paper endpoint, and saves them to monitor/exec.env. Paper only: live keys are rejected here.
# Safe to re-run. Usage: sudo bash /opt/rotation-desk-feed/ops/set-exec-keys.sh
set -u
cd "$(dirname "$0")/../monitor"
ask() {
  local v
  while true; do
    read -rsp "$1 (hidden, paste then Enter): " v; echo
    v="${v//[[:space:]]/}"
    if [ -n "$v" ]; then echo "  received ${#v} characters"; printf -v "$2" '%s' "$v"; return; fi
    echo "  nothing received, try again (Ctrl+C to quit)"
  done
}
ask "NEW paper account - API key ID" K
ask "NEW paper account - secret key" S
R=$(curl -s -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" https://paper-api.alpaca.markets/v2/account)
INFO=$(printf '%s' "$R" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("ERR could not read the reply"); sys.exit(0)
if "account_number" not in d:
    print("ERR " + str(d.get("message", d))[:120]); sys.exit(0)
print("OK account ending " + d["account_number"][-4:] + ", status " + d.get("status", "?") + ", equity $" + d.get("equity", "?"))')
case "$INFO" in
  OK*) ;;
  *) echo "NOT SAVED: these keys did not work on the PAPER endpoint ($INFO)."; echo "Use keys from the paper account's page in Alpaca, then re-run."; unset K S; exit 1 ;;
esac
umask 077
printf 'EXEC_MODE=paper\nEXEC_KEY_ID=%s\nEXEC_SECRET_KEY=%s\n' "$K" "$S" > exec.env
unset K S
id deskmon >/dev/null 2>&1 && chown deskmon:deskmon exec.env
echo "SAVED. Paper ${INFO#OK }"
echo "Check that this is the NEW account, not the one your existing bot uses."

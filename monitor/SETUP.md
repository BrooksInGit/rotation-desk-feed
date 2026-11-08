# Server setup (DigitalOcean, Ubuntu)

Runs next to your existing paper bot without touching it: separate user, folder and service.
Needs Python 3.10+ (Ubuntu 22.04 or newer has it). Takes about 15 minutes.

Commands are run over SSH as your normal sudo user. Replace nothing unless a step says so.

## 1. Packages and a dedicated user

```bash
sudo apt update && sudo apt install -y git python3-venv
sudo useradd --system --create-home --home-dir /home/deskmon --shell /bin/bash deskmon
```

## 2. A key that can only reach this one repo

```bash
sudo -u deskmon ssh-keygen -t ed25519 -N "" -C "desk-monitor" -f /home/deskmon/.ssh/rotation_desk_feed
sudo -u deskmon bash -c 'cat > ~/.ssh/config <<EOF
Host github.com
  IdentityFile ~/.ssh/rotation_desk_feed
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config'
sudo cat /home/deskmon/.ssh/rotation_desk_feed.pub
```

Copy the line that starts with `ssh-ed25519`. On GitHub open **rotation-desk-feed → Settings → Deploy keys → Add deploy key**,
paste it, title it `desk-monitor`, and tick **Allow write access** (the monitor commits its event files). Save.

## 3. Get the code

```bash
sudo mkdir -p /opt/rotation-desk-feed && sudo chown deskmon:deskmon /opt/rotation-desk-feed
sudo -u deskmon ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # "successfully authenticated" is expected
sudo -u deskmon git clone git@github.com:BrooksInGit/rotation-desk-feed.git /opt/rotation-desk-feed
sudo -u deskmon python3 -m venv /opt/rotation-desk-feed/monitor/.venv
sudo -u deskmon /opt/rotation-desk-feed/monitor/.venv/bin/pip install -q -r /opt/rotation-desk-feed/monitor/requirements.txt
```

## 4. Your Telegram chat ID

Send your bot any message in Telegram first, then (paste your bot token where shown):

```bash
read -s -p "Bot token: " TG; echo
curl -s "https://api.telegram.org/bot$TG/getUpdates" | python3 -c 'import sys,json; [print("chat id:", u["message"]["chat"]["id"]) for u in json.load(sys.stdin)["result"] if "message" in u]'
unset TG
```

## 5. Settings file

```bash
cd /opt/rotation-desk-feed/monitor
sudo -u deskmon cp .env.example .env
sudo -u deskmon chmod 600 .env
sudo -u deskmon nano .env
```

Fill in `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` (the same keys your paper bot uses are fine), `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID`. Leave the rest. Save with Ctrl+O, Enter, Ctrl+X.

## 6. Test

```bash
cd /opt/rotation-desk-feed/monitor
sudo -u deskmon bash -c 'set -a; . ./.env; set +a; .venv/bin/python test_core.py && .venv/bin/python monitor.py --test'
```

You should get a Telegram message, plus lines confirming the SIP stock feed and the OPRA options feed.

## 7. Run it all day, every day

```bash
sudo cp /opt/rotation-desk-feed/monitor/desk-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now desk-monitor
sudo systemctl status desk-monitor --no-pager
```

Live log: `sudo journalctl -u desk-monitor -f` (Ctrl+C to stop watching; the monitor keeps running).

## Later

- **Code updates:** `sudo -u deskmon git -C /opt/rotation-desk-feed pull && sudo systemctl restart desk-monitor`
  (the watchlist itself refreshes automatically every 30 seconds; only code changes need the restart).
- **Stop it:** `sudo systemctl stop desk-monitor` (and `disable` to keep it off after reboots).
- **Is it alive?** `status.json` in the repo updates at least every 5 minutes while it runs.

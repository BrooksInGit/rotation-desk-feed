#!/usr/bin/env bash
# Re-runs both play replays (with stock legs) and commits. Usage: bash research/run_replays.sh
set -e
cd /opt/rotation-desk-feed
git pull -q
set -a; . monitor/.env; set +a
monitor/.venv/bin/python research/replay.py research/plays.json research/radar.json research/replay-out.json > /tmp/replay.log 2>&1
REPLAY_IGNORE_RVOL=1 monitor/.venv/bin/python research/replay.py research/plays.json research/radar.json research/replay-norvol.json > /tmp/replay2.log 2>&1
git add research/replay-out.json research/replay-norvol.json
git -c user.name=desk-monitor -c user.email=desk-monitor@localhost commit -qm "research: replays with stock legs"
git pull -q --rebase && git push -q && echo REPLAYS_PUSHED >> /tmp/replay2.log

#!/usr/bin/env bash
# Scores live RVOL-blocked scalps and commits. Usage: bash research/run_rvol_blocked.sh
set -e
cd /opt/rotation-desk-feed
git pull -q
set -a; . monitor/.env; set +a
monitor/.venv/bin/python research/rvol_blocked.py > /tmp/rvol_blocked.log 2>&1
git add research/rvol_blocked.json
git -c user.name=desk-monitor -c user.email=desk-monitor@localhost commit -qm "research: score RVOL-blocked live scalps"
git pull -q --rebase && git push -q && echo RVOL_PUSHED >> /tmp/rvol_blocked.log

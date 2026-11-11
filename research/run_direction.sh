#!/usr/bin/env bash
# Market direction test. Usage: bash research/run_direction.sh
set -e
cd /opt/rotation-desk-feed
git pull -q
set -a; . monitor/.env; set +a
monitor/.venv/bin/python research/direction_test.py > /tmp/dir.log 2>&1
git add research/direction_test.json
git -c user.name=desk-monitor -c user.email=desk-monitor@localhost commit -qm "research: market direction at scalp entries"
git pull -q --rebase && git push -q && echo DIR_PUSHED >> /tmp/dir.log

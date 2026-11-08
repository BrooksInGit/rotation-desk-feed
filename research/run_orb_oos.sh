#!/usr/bin/env bash
# Runs the out-of-sample ORB test on the server and commits the results. Usage: bash research/run_orb_oos.sh
set -e
cd /opt/rotation-desk-feed
git pull -q
monitor/.venv/bin/pip install -q numpy
set -a; . monitor/.env; set +a
monitor/.venv/bin/python research/orb_oos.py > /tmp/orb_oos.log 2>&1
cp /tmp/orb_oos.log research/orb_oos/run.log
git add research/orb_oos
git -c user.name=desk-monitor -c user.email=desk-monitor@localhost commit -qm "research: ORB out-of-sample results"
git pull -q --rebase && git push -q && echo ORB_PUSHED >> /tmp/orb_oos.log

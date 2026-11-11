#!/usr/bin/env bash
# Stockbee swing setups out of sample (momentum burst, episodic pivot). Usage: sudo -u deskmon bash research/run_swing.sh
set -e
cd /opt/rotation-desk-feed
git pull -q
set -a; . monitor/.env; set +a
nice -n 10 monitor/.venv/bin/python research/swing_oos.py > /tmp/swing.log 2>&1
cp /tmp/swing.log research/swing_oos/run.log
git add research/swing_oos/summary.json research/swing_oos/trades.csv.gz research/swing_oos/run.log
git -c user.name=desk-monitor -c user.email=desk-monitor@localhost commit -qm "research: Stockbee swing setups out of sample"
git pull -q --rebase && git push -q && echo SWING_PUSHED >> /tmp/swing.log

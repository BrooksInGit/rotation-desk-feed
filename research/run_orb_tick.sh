#!/usr/bin/env bash
set -e
cd /opt/rotation-desk-feed
git pull -q
set -a; . monitor/.env; set +a
monitor/.venv/bin/python research/orb_oos_tick.py > /tmp/orb_tick.log 2>&1
cp /tmp/orb_tick.log research/orb_oos/run_tick.log
git add research/orb_oos
git -c user.name=desk-monitor -c user.email=desk-monitor@localhost commit -qm "research: ORB out-of-sample, tick-resolved entries"
git pull -q --rebase && git push -q && echo TICK_PUSHED >> /tmp/orb_tick.log

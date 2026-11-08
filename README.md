# rotation-desk-feed

The live layer for the Rotation Desk (paper mode).

- `watchlist.json`: active trigger levels, written by the desk's scheduled runs.
- `monitor/`: the always-on monitor that streams Alpaca SIP trades for those symbols, checks the desk's
  confirmation rules, captures live OPRA option quotes at the trigger, sends Telegram alerts, and writes
  `events/` back here. It never places orders.
- `events/`, `status.json`: written by the monitor.

Format: see `WATCHLIST_FORMAT.md`. Server setup: see `monitor/SETUP.md`.
Tests: `cd monitor && python3 test_core.py`.

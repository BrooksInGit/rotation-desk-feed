# watchlist.json format

Written by the Rotation Desk's scheduled runs (evening scan, pre-market brief, hourly check).
Read by the monitor every 30 seconds. Only active items are watched.

```json
{
  "updated_at": "2026-11-09T21:40:00Z",
  "source": "evening scan",
  "slippage_frac": 0.25,
  "watch": [ ITEM, ... ]
}
```

## Item

| field | required | meaning |
|---|---|---|
| `id` | yes | Unique and stable: `<play_id>:<kind>`, e.g. `20261109-swing-XLE-C:entry` |
| `play_id` | yes | The play's id in the tracker database |
| `symbol` | yes | Underlying stock or ETF |
| `kind` | yes | `entry`, `stop`, `target` (stock price) or `option_stop`, `option_target` (option mid price) |
| `status` | no | `active` (default) or anything else to ignore |
| `expires` | no | `YYYY-MM-DD`; ignored after this date |
| `armed_after` | no | ISO time; trades before this don't count (use for intraday-created items) |
| `module`, `setup`, `direction` | yes | `swing`/`scalp`; setup name; `call`/`put` |
| `trigger` | yes | `{"type":"above"\|"below","price":95.2}` or `{"type":"orb_high"\|"orb_low","minutes":30}`; for option kinds, `price` is the option premium |
| `confirmation` | entry | `{"rvol_min":1.5,"vwap":"above","max_extension_pct":0.025,"spy_block_pct":0.0075,"skip_first_minutes":15}` |
| `option_symbol` | when known | OCC symbol, e.g. `XLE261219C00096000`. Required for option kinds |
| `contract_rule` | scalps | `{"dte":[0,7],"delta":[0.4,0.6],"max_spread_pct":0.05,"max_premium":200}`; used when `option_symbol` is empty |
| `plan` | no | One line shown in the alert |

## What the monitor writes back

- `events/YYYY-MM-DD.jsonl`: one JSON line per trigger, stop or target, with `time`, `crossed_at`, `price`,
  `checks`, `confirmed`, `session_vwap`, `session_volume`, and `option` (`symbol`, `bid`, `ask`, `mid`,
  `spread_pct`, `delta`, `fill_est`, `fill_side`). The desk uses `option.fill_est` as the paper fill.
- `status.json`: heartbeat (`updated_at`, `stream_connected`, `market_open`, `watching`).

## Added 2026-11-10 (manual 11.6 and 12 E)
- Optional on `entry` items: `stock_stop`, `target_stock` (from the play's plan). The monitor uses them for the share leg; without them it uses `exit_rules`.
- Optional at the top level: `exit_rules` = `{"scalp": {"option_stop_pct":0.28,"option_target_mult":1.40,"stock_stop_pct":0.01,"stock_target_r":2.0}, "swing": {...}}`.
- On every confirmed entry the monitor arms its own exits: `<play_id>:auto:option_stop|option_target` (leg `option`) and `<play_id>:auto:stop|target` (leg `shares`), and at 15:05 ET closes open scalp legs with `time_exit` events. Exit events carry `leg`, `entry_event` and, on the entry event, `exits` (the armed levels). Desk-published exit items for a play the monitor holds are ignored.
- When an entry fails only the volume check, the monitor records a tracked twin: an `entry` event with `item_id` `<id>:no_rvol`, `variant: "no_rvol"`, `track_only: true`, `confirmed: true`, and arms its exits under play id `<play_id>-no_rvol`. These send no Telegram alerts.
- The monitor restarts itself when anything under `monitor/` changes on `main`.

## Added 2026-11-11: pullback-to-level reclaim (manual 15)
`trigger: {"type": "reclaim_above" | "reclaim_below", "zone": [lo, hi], "window": ["09:45", "11:30"]}`.
The item arms when price first trades into the zone inside the window, then triggers when price trades back
above max(session VWAP, zone high) (calls) or below min(session VWAP, zone low) (puts) while still inside the window.
Give it a `stock_stop` just beyond the zone. Set `track_only: true` while the setup is being measured.

## Added 2026-11-17: live intraday discovery (manual 14)
Optional top-level `scan` block. Published by the pre-market brief; every later run keeps it when it rewrites the file.

```json
"scan": {
  "symbols": {"NVDA": {"group": "SMH"}, "SMH": {}, "SPY": {"index": true}, "MRVL": {"lane": true}},
  "exclude": ["WIX"],
  "params": {"rvol_min": 1.0, "base_minutes": 30, "max_base_depth_pct": 1.5, "min_move_pct": 1.0,
             "max_new_per_scan": 2, "max_new_per_day": 6, "start": "10:00", "cutoff": "14:30",
             "execute": false, "telegram": false}
}
```

- `symbols`: the scan list. Direction is the stock's own move (manual 17); by default only names moving the same way as SPY count (`with_spy`). `group` is the name's sector ETF, used for the `sector_today` tag (and as a filter only if `with_group: true`); `lane: true`
  lets a momentum-lane name count in either direction, `index: true` halves the move needed.
- `exclude`: names to skip today (earnings today, anything the desk does not want).
- `params`: any of the defaults in `desk_core.SCAN_DEFAULTS`.
- From `start` to `cutoff` ET, once a minute, the monitor takes the `top_n` biggest movers and checks completed
  1-minute bars for the intraday_break setup: same-time volume ≥ `rvol_min`, price on the trade's side of VWAP, the
  high (calls) or low (puts) of day set at least `base_minutes` ago, and every bar since within
  `max_base_depth_pct` of it. Names with an active desk entry are skipped, and a name is found at most once a day.
- For each find (best volume first, at most `max_new_per_scan` per minute and `max_new_per_day` per day) it writes a
  `discovery` event (no `item_id`; the armed item is in `item`) and starts watching the entry itself:
  id `<YYYYMMDD>-scalp-<SYM>-<C|P>-intra:entry`, trigger = the high/low of day, `stock_stop` = the base low/high,
  `target_stock` = 2R, `armed_after` = the find time. Its entries and exits are ordinary events.
- `execute: false` (default) arms it `track_only`: measured, never ordered. `true` lets a confirmed entry go to the
  paper executor like any desk entry. `telegram: true` sends a line on each find.
- The desk does not publish these items in `watchlist.json`; the monitor holds them and restores them from the
  events file after a restart. `status.json` shows `scan.on` and `scan.found_today`.

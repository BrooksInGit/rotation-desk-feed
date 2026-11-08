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

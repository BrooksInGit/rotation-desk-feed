#!/usr/bin/env python3
"""Second pass for orb_oos.py: re-score every trade whose entry minute also touched the stop, using SIP
trade-by-trade prints for that minute (which came first: the breakout or the stop?). Then re-summarize.
Writes research/orb_oos/trades_tick.json.gz and summary_tick.json."""
from __future__ import annotations

import gzip
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "monitor"))
import orb_oos as o  # noqa: E402
from desk_core import EXCLUDED_CONDITIONS  # noqa: E402

OUT = o.OUT


def prints(sym, start, end):
    out, params = [], {"start": start, "end": end, "feed": "sip", "limit": 10000}
    while True:
        j = o.get(f"/v2/stocks/{sym}/trades", params)
        out += j.get("trades") or []
        if not j.get("next_page_token"):
            return out
        params["page_token"] = j["next_page_token"]


def ok(t):
    return not any(c in EXCLUDED_CONDITIONS for c in (t.get("c") or []))


def rescore(t):
    if t["how"] != "stop same bar":
        return t
    side, sym = t["side"], t["sym"]
    minute = datetime.fromisoformat(t["entry_t"].replace("Z", "+00:00"))
    hi_lo_level = t["entry"]  # bar-level entry estimate; recompute from prints
    pk = prints(sym, o.utc(minute), o.utc(minute + timedelta(minutes=1)))
    # breakout level = the 5-minute high/low; the bar-level entry was max(level, open), so the level is <= entry
    lvl = t.get("level", hi_lo_level)
    entry = None
    for p in pk:
        if not ok(p):
            continue
        px = float(p["p"])
        if entry is None:
            if (side == "long" and px >= lvl) or (side == "short" and px <= lvl):
                entry, stop = px, (px - t["stop_d"] if side == "long" else px + t["stop_d"])
            continue
        if (side == "long" and px <= stop) or (side == "short" and px >= stop):
            return dict(t, entry=entry, exit=stop, how="stop (tick)", exit_t=p["t"])
    if entry is None:
        return dict(t, how="no entry (tick)")
    # survived its entry minute: continue on 1-minute bars to the close
    dt = minute.astimezone(o.ET)
    b = o.bars([sym], "1Min", o.utc(minute + timedelta(minutes=1)), o.utc(dt.replace(hour=16, minute=0))).get(sym, [])
    stop = entry - t["stop_d"] if side == "long" else entry + t["stop_d"]
    for x in b:
        if (side == "long" and x["l"] <= stop) or (side == "short" and x["h"] >= stop):
            ex = min(stop, x["o"]) if side == "long" else max(stop, x["o"])
            return dict(t, entry=entry, exit=ex, how="stop (after tick entry)", exit_t=x["t"])
    if b:
        return dict(t, entry=entry, exit=b[-1]["c"], how="close (after tick entry)", exit_t=b[-1]["t"])
    return dict(t, entry=entry, exit=entry, how="no bars after entry")


def main():
    trades = json.load(gzip.open(OUT / "trades.json.gz", "rt"))
    picks = json.load(open(OUT / "picks.json"))
    lvl = {(d, s): (ob[1] if side == "long" else ob[2]) for d, ps in picks.items() for s, side, ob, a, rv in ps}
    for t in trades:
        t["level"] = lvl.get((t["day"], t["sym"]), t["entry"])
    todo = [t for t in trades if t["how"] == "stop same bar"]
    o.log("re-scoring", len(todo), "same-bar trades with tick data")
    done = []
    with ThreadPoolExecutor(8) as ex:
        for n, r in enumerate(ex.map(rescore, trades)):
            done.append(r)
            if n % 1000 == 0:
                o.log("tick pass", n, "/", len(trades))
    done = [t for t in done if t["how"] not in ("no entry (tick)",)]
    json.dump(done, gzip.open(OUT / "trades_tick.json.gz", "wt"))
    days = sorted({t["day"] for t in done})
    summ = o.summarize(done, days)
    from collections import Counter
    summ["exit_types"] = dict(Counter(t["how"] for t in done))
    json.dump(summ, open(OUT / "summary_tick.json", "w"), indent=1)
    o.log("done", json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("by_month",)} for k, v in summ.items() if k.startswith("slip")}))


if __name__ == "__main__":
    main()

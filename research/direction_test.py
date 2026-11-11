#!/usr/bin/env python3
"""Does market direction at entry separate scalp winners from losers?
For every scalp in research/rvol_blocked.json (live 11/9-) and replay-norvol.json (10/16-11/6, stock leg), records SPY at
the entry minute: % vs prior close, % vs today's open, side of session VWAP, and SPY vs its 20-day EMA; plus the stock's
own % vs prior close and side of its own VWAP. "Aligned" = the market measure points the trade's way.
Writes research/direction_test.json."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "monitor"))
import desk_core as c  # noqa: E402

S = requests.Session()
S.headers.update({"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
CACHE: dict = {}


def bars(sym, tf, start, end):
    k = (sym, tf, start, end)
    if k in CACHE:
        return CACHE[k]
    out, params = [], {"timeframe": tf, "start": start, "end": end, "feed": "sip", "limit": 10000, "adjustment": "raw"}
    while True:
        j = S.get(f"https://data.alpaca.markets/v2/stocks/{sym}/bars", params=params, timeout=30).json()
        out += j.get("bars") or []
        if not j.get("next_page_token"):
            break
        params["page_token"] = j["next_page_token"]
    CACHE[k] = out
    return out


def state(sym, t):
    d = t.date()
    day = bars(sym, "1Min", f"{d}T13:00:00Z", f"{d}T21:00:00Z")
    rth = [b for b in day if c.in_rth(c.to_et(b["t"]))]
    upto = [b for b in rth if c.to_et(b["t"]) < t.replace(second=0, microsecond=0)] or rth[:1]
    if not upto:
        return None
    px = upto[-1]["c"]
    pv = sum(b["v"] for b in upto) or 1
    vwap = sum(b["vw"] * b["v"] for b in upto) / pv
    daily = bars(sym, "1Day", f"{d - timedelta(days=45)}", f"{d - timedelta(days=1)}")
    prev = daily[-1]["c"] if daily else rth[0]["o"]
    ema = None
    if len(daily) >= 20:
        k = 2 / 21
        ema = daily[0]["c"]
        for b in daily[1:]:
            ema = b["c"] * k + ema * (1 - k)
    return {"px": px, "chg": round(px / prev - 1, 4), "from_open": round(px / rth[0]["o"] - 1, 4),
            "above_vwap": px > vwap, "above_ema20": (prev > ema) if ema else None}


def main():
    rows = []
    for x in json.load(open(ROOT / "research" / "rvol_blocked.json")):
        rows.append({"src": "live", "id": x["play"], "sym": x["sym"], "side": x["side"], "setup": x["setup"],
                     "time": x["time"], "rvol": x["rvol"], "r": x["r"]})
    for x in json.load(open(ROOT / "research" / "replay-norvol.json")):
        if x.get("module") != "scalp" or not x.get("stock") or not x.get("confirmed_at"):
            continue
        rows.append({"src": "replay", "id": x["id"], "sym": x["underlying"], "side": x["direction"], "setup": x["setup"],
                     "time": x["confirmed_at"], "rvol": x.get("rvol_at_entry"), "r": x["stock"]["r"]})
    for r in rows:
        t = c.to_et(r["time"])
        r["spy"] = state("SPY", t)
        r["own"] = state(r["sym"], t)
        print(r["id"], r["side"], r["r"], r["spy"] and r["spy"]["chg"], flush=True)
    json.dump(rows, open(ROOT / "research" / "direction_test.json", "w"), indent=1)


if __name__ == "__main__":
    main()

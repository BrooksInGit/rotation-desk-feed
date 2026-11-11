#!/usr/bin/env python3
"""Scores every live scalp entry the monitor logged (Nov 9 onward) as a share trade under the scalp exit rules
(1% stop, 2R target, 15:05 close), grouped by whether RVOL was the only check that blocked it.
Entry = the first logged crossing for a play where nothing but RVOL failed (or the confirmed one), price on the
right side of the trigger. Writes research/rvol_blocked.json."""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "monitor"))
import desk_core as c  # noqa: E402

S = requests.Session()
S.headers.update({"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
STOP, TGT = 0.01, 0.02


def bars(sym, start, end):
    out, params = [], {"timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(), "feed": "sip",
                       "limit": 10000, "adjustment": "raw"}
    while True:
        j = S.get(f"https://data.alpaca.markets/v2/stocks/{sym}/bars", params=params, timeout=30).json()
        out += j.get("bars") or []
        if not j.get("next_page_token"):
            return out
        params["page_token"] = j["next_page_token"]


def score(sym, side, t0, px):
    end = t0.replace(hour=15, minute=5, second=0, microsecond=0)
    start = (t0 + timedelta(minutes=1)).replace(second=0, microsecond=0)
    b = bars(sym, start, end)
    long = side == "call"
    stop = px * (1 - STOP) if long else px * (1 + STOP)
    tgt = px * (1 + TGT) if long else px * (1 - TGT)
    mfe = 0.0
    for x in b:
        if (long and x["l"] <= stop) or (not long and x["h"] >= stop):
            return {"how": "stop", "r": -1.0 if (long and x["o"] > stop) or (not long and x["o"] < stop)
                    else round(((x["o"] - px) if long else (px - x["o"])) / (px * STOP), 2), "exit_t": x["t"], "mfe_r": mfe}
        if (long and x["h"] >= tgt) or (not long and x["l"] <= tgt):
            return {"how": "target", "r": 2.0, "exit_t": x["t"], "mfe_r": 2.0}
        mfe = max(mfe, round(((x["h"] - px) if long else (px - x["l"])) / (px * STOP), 2))
    if not b:
        return {"how": "no bars", "r": 0.0, "mfe_r": 0.0}
    last = b[-1]["c"]
    return {"how": "15:05", "r": round(((last - px) if long else (px - last)) / (px * STOP), 2), "exit_t": b[-1]["t"], "mfe_r": mfe}


def main():
    out = []
    for f in sorted(glob.glob(str(ROOT / "events" / "*.jsonl"))):
        seen = set()
        for line in open(f):
            e = json.loads(line)
            if e.get("kind") != "entry" or e.get("module") != "scalp" or e.get("variant"):
                continue
            pid = e["play_id"]
            if pid in seen:
                continue
            fails = {k["name"] for k in e.get("checks", []) if not k["ok"]}
            if not (e.get("confirmed") or fails == {"rvol"}):
                continue
            lvl, px, d = e.get("trigger_level"), e["price"], e["direction"]
            if lvl and ((d == "call" and px < lvl) or (d == "put" and px > lvl)):
                continue
            seen.add(pid)
            rv = next((k["note"] for k in e.get("checks", []) if k["name"] == "rvol"), "")
            m = re.search(r"([\d.]+)x", rv)
            t0 = c.to_et(e["time"])
            r = score(e["symbol"], d, t0, px)
            out.append({"play": pid, "sym": e["symbol"], "setup": e["setup"], "side": d, "time": e["time"],
                        "price": px, "rvol": float(m.group(1)) if m else None,
                        "blocked_by_rvol": not e.get("confirmed"), **r})
            print(pid, out[-1]["time"][11:16], out[-1]["rvol"], r["how"], r["r"], flush=True)
    json.dump(out, open(ROOT / "research" / "rvol_blocked.json", "w"), indent=1)


if __name__ == "__main__":
    main()

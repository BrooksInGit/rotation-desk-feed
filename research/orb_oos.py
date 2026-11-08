#!/usr/bin/env python3
"""Out-of-sample replication of Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy
For The U.S. Equity Market": 5-minute opening-range breakout on the top 20 "stocks in play".

Rules (as published):
  universe   US-listed common stocks; open > $5, 14-day avg volume >= 1M shares, 14-day ATR > $0.50
  in play    relative volume of the 9:30-9:35 bar vs its prior 14-day average >= 1.0; top 20 by it
  direction  long if the first 5-minute candle closed up, short if down, none on a doji
  entry      stop order at the 5-minute high (long) / low (short), from 9:35
  stop       10% of the 14-day ATR from entry
  exit       stop, else the 4:00 PM close
  sizing     1% of equity risked per trade, notional capped so 20 positions use at most 4x

Data: Alpaca SIP (all exchanges). Period: 2024-01-02 to END. Writes trades and summaries to research/orb_oos/.
Limits: active symbols only (delisted names missing = survivorship bias); ETFs removed by name heuristic;
1-minute bars decide fills (a bar that touches both entry and stop is counted as a loss).

Run on the server: python3 research/orb_oos.py   (resumable; caches each phase)
"""
from __future__ import annotations

import gzip
import json
import math
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

ET = ZoneInfo("America/New_York")
DATA = "https://data.alpaca.markets"
OUT = Path(__file__).resolve().parent / "orb_oos"
OUT.mkdir(exist_ok=True)
START, END = date(2024, 1, 2), date.fromisoformat(os.environ.get("ORB_END", "2026-11-06"))
WARM = date(2023, 11, 1)
TOP_N, RISK, MAX_LEV = 20, 0.01, 4.0
SLIPS = (0.0, 0.01, 0.02)  # dollars per share, each way

S = requests.Session()
S.headers.update({"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
ETF_WORDS = ("ETF", " FUND", "TRUST", "ISHARES", "SPDR", "PROSHARES", "DIREXION", "INVESCO", "VANGUARD", "GRANITESHARES",
             "LEVERAGED", "2X", "3X", "-1X", "INVERSE", "BULL", "BEAR", "ETN", "INDEX", "YIELDMAX", "T-REX", "DEFIANCE",
             "ROUNDHILL", "TRADR", "LEVERAGE SHARES", "ULTRA", "MICROSECTORS", "WISDOMTREE", "GLOBAL X", "VANECK", "SCHWAB",
             "FIRST TRUST", "XTRACKERS", "AMPLIFY", "ARK ", "KRANESHARES", "SPROTT", "ABRDN", "PIMCO", "JPMORGAN", "FIDELITY")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def get(path, params):
    for i in range(6):
        try:
            r = S.get(DATA + path, params=params, timeout=60)
        except requests.RequestException:
            time.sleep(2 + i)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 + 2 * i)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"failed {path}")


def utc(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def et(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ET)


def cache(name, fn):
    p = OUT / f"{name}.json.gz"
    if p.exists():
        return json.load(gzip.open(p, "rt"))
    v = fn()
    json.dump(v, gzip.open(p, "wt"))
    return v


def bars(symbols, timeframe, start, end, adjustment="raw"):
    out = {}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
              "feed": "sip", "limit": 10000, "adjustment": adjustment}
    while True:
        j = get("/v2/stocks/bars", params)
        for s, bs in (j.get("bars") or {}).items():
            out.setdefault(s, []).extend(bs)
        if not j.get("next_page_token"):
            return out
        params["page_token"] = j["next_page_token"]


# ---------------------------------------------------------------- phase 1: universe
def phase_universe():
    r = requests.get("https://paper-api.alpaca.markets/v2/assets", headers=S.headers,
                     params={"status": "active", "asset_class": "us_equity"}, timeout=60).json()
    keep = []
    for a in r:
        if a.get("exchange") not in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
            continue
        name = (a.get("name") or "").upper()
        sym = a["symbol"]
        if any(w in name for w in ETF_WORDS) or "." in sym or "/" in sym or len(sym) > 5:
            continue
        keep.append(sym)
    log("universe", len(keep))
    return sorted(keep)


# ---------------------------------------------------------------- phase 2: daily eligibility (numpy, low memory)
def phase_daily(universe):
    """ATR matrix [symbol, day] (float32, NaN when not eligible) for START..END, plus the day list."""
    spy = bars(["SPY"], "1Day", START.isoformat(), END.isoformat())["SPY"]
    days = [x["t"][:10] for x in spy]
    di = {d: i for i, d in enumerate(days)}
    atr = np.full((len(universe), len(days)), np.nan, dtype=np.float32)
    si = {s: i for i, s in enumerate(universe)}
    batches = [universe[i:i + 100] for i in range(0, len(universe), 100)]

    def work(batch):
        b = bars(batch, "1Day", WARM.isoformat(), END.isoformat(), adjustment="split")
        res = []
        for s, bs in b.items():
            o = np.array([x["o"] for x in bs]); h = np.array([x["h"] for x in bs]); l = np.array([x["l"] for x in bs])
            c = np.array([x["c"] for x in bs]); v = np.array([x["v"] for x in bs], dtype=float); t = [x["t"][:10] for x in bs]
            if len(bs) < 16:
                continue
            pc = np.concatenate([[c[0]], c[:-1]])
            tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
            for i in range(15, len(bs)):
                if t[i] not in di:
                    continue
                a = tr[i - 14:i].mean(); av = v[i - 14:i].mean()
                if o[i] > 5 and av >= 1_000_000 and a > 0.5:
                    res.append((si[s], di[t[i]], a))
        return res

    with ThreadPoolExecutor(6) as ex:
        for n, res in enumerate(ex.map(work, batches)):
            for r, k, a in res:
                atr[r, k] = a
            if n % 10 == 0:
                log("daily batch", n, "/", len(batches))
    return days, atr


# ---------------------------------------------------------------- phase 3+4: opening bars and selection, day by day
def phase_select(universe, days, atr):
    picks_p = OUT / "picks.json"
    vol_p = OUT / "openvol.npy"
    picks = json.load(open(picks_p)) if picks_p.exists() else {}
    V = np.load(vol_p) if vol_p.exists() else np.full(atr.shape, np.nan, dtype=np.float32)
    elig_any = ~np.isnan(atr)
    for k, d in enumerate(days):
        if d in picks:
            continue
        window = elig_any[:, k:k + 15].any(axis=1)
        need = [universe[i] for i in np.nonzero(window)[0]]
        dt = datetime.fromisoformat(d).replace(tzinfo=ET)
        st_, en = utc(dt.replace(hour=9, minute=30)), utc(dt.replace(hour=9, minute=35))
        batches = [need[i:i + 200] for i in range(0, len(need), 200)]
        ohlc = {}
        with ThreadPoolExecutor(8) as ex:
            for b in ex.map(lambda bt: bars(bt, "5Min", st_, en), batches):
                for s, bs in b.items():
                    for x in bs[:1]:
                        if et(x["t"]).strftime("%H:%M") == "09:30":
                            ohlc[s] = [x["o"], x["h"], x["l"], x["c"], x["v"]]
        for s, ob in ohlc.items():
            V[universe_index[s], k] = ob[4]
        cands = []
        for i in np.nonzero(elig_any[:, k])[0]:
            s = universe[i]
            if s not in ohlc or k < 10:
                continue
            hist = V[i, max(0, k - 14):k]
            hist = hist[~np.isnan(hist)]
            if len(hist) < 10 or hist.mean() <= 0:
                continue
            rv = ohlc[s][4] / hist.mean()
            o, c = ohlc[s][0], ohlc[s][3]
            if rv >= 1.0 and c != o:
                cands.append((float(rv), s, "long" if c > o else "short", ohlc[s], float(atr[i, k])))
        cands.sort(reverse=True)
        picks[d] = [[s, side, ob, a, round(rv, 2)] for rv, s, side, ob, a in cands[:TOP_N]]
        if k % 25 == 0 or k == len(days) - 1:
            json.dump(picks, open(picks_p, "w")); np.save(vol_p, V)
            log("selection day", k, "/", len(days), d, "candidates", len(cands))
    json.dump(picks, open(picks_p, "w")); np.save(vol_p, V)
    return picks


# ---------------------------------------------------------------- phase 5: simulate
def phase_sim(picks):
    trades = []
    days = sorted(picks)

    def work(d):
        p = picks[d]
        if not p:
            return []
        dt = datetime.fromisoformat(d).replace(tzinfo=ET)
        b = bars([x[0] for x in p], "1Min", utc(dt.replace(hour=9, minute=35)), utc(dt.replace(hour=16, minute=0)))
        res = []
        for s, side, ob, atr, rv in p:
            mb = b.get(s, [])
            hi, lo = ob[1], ob[2]
            lvl = hi if side == "long" else lo
            stop_d = 0.10 * atr
            entry = None
            for i, x in enumerate(mb):
                if entry is None:
                    if (side == "long" and x["h"] >= lvl) or (side == "short" and x["l"] <= lvl):
                        entry = max(lvl, x["o"]) if side == "long" else min(lvl, x["o"])
                        stop = entry - stop_d if side == "long" else entry + stop_d
                        et_ = x["t"]
                        # same bar: if the bar also reaches the stop, count the stop (conservative)
                        if (side == "long" and x["l"] <= stop) or (side == "short" and x["h"] >= stop):
                            res.append(dict(day=d, sym=s, side=side, rv=rv, entry=entry, stop_d=stop_d, exit=stop,
                                            how="stop same bar", entry_t=et_, exit_t=x["t"], price=entry, atr=atr))
                            break
                    continue
                if (side == "long" and x["l"] <= stop) or (side == "short" and x["h"] >= stop):
                    ex_ = min(stop, x["o"]) if side == "long" else max(stop, x["o"])
                    res.append(dict(day=d, sym=s, side=side, rv=rv, entry=entry, stop_d=stop_d, exit=ex_, how="stop",
                                    entry_t=et_, exit_t=x["t"], price=entry, atr=atr))
                    break
            else:
                if entry is not None and mb:
                    res.append(dict(day=d, sym=s, side=side, rv=rv, entry=entry, stop_d=stop_d, exit=mb[-1]["c"], how="close",
                                    entry_t=et_, exit_t=mb[-1]["t"], price=entry, atr=atr))
        return res

    with ThreadPoolExecutor(8) as ex:
        for n, res in enumerate(ex.map(work, days)):
            trades += res
            if n % 50 == 0:
                log("sim day", n, "/", len(days), "trades", len(trades))
    return trades


# ---------------------------------------------------------------- phase 6: results
def summarize(trades, days):
    out = {}
    for slip in SLIPS:
        eq, peak, mdd = 25000.0, 25000.0, 0.0
        daily = {}
        rs = []
        for d in days:
            dt_ = [t for t in trades if t["day"] == d]
            pnl = 0.0
            for t in dt_:
                sign = 1 if t["side"] == "long" else -1
                per_share = sign * (t["exit"] - t["entry"]) - 2 * slip
                risk_sh = t["stop_d"] + 2 * slip
                shares = math.floor(min(RISK * eq / risk_sh, (MAX_LEV * eq / TOP_N) / t["price"]))
                pnl += shares * per_share
                rs.append(per_share / t["stop_d"])
            daily[d] = pnl / eq
            eq += pnl
            peak = max(peak, eq)
            mdd = max(mdd, 1 - eq / peak)
        rets = list(daily.values())
        weeks, months = {}, {}
        for d, r in daily.items():
            dd = date.fromisoformat(d)
            weeks.setdefault(dd.isocalendar()[:2], []).append(r)
            months.setdefault(d[:7], []).append(r)
        wk = [math.prod(1 + x for x in v) - 1 for v in weeks.values()]
        mo = [math.prod(1 + x for x in v) - 1 for v in months.values()]
        years = {}
        for d, r in daily.items():
            years.setdefault(d[:4], []).append(r)
        streak = cur = 0
        for v in mo:
            cur = cur + 1 if v < 0 else 0
            streak = max(streak, cur)
        out[f"slip_{slip:.2f}"] = {
            "final_equity_from_25k": round(eq), "total_return": round(eq / 25000 - 1, 3),
            "sharpe": round(st.mean(rets) / st.pstdev(rets) * math.sqrt(252), 2) if st.pstdev(rets) else None,
            "max_drawdown": round(mdd, 3), "trades": len(rs), "avg_R": round(st.mean(rs), 3) if rs else None,
            "hit_rate": round(sum(r > 0 for r in rs) / len(rs), 3) if rs else None,
            "winning_weeks": round(sum(x > 0 for x in wk) / len(wk), 3), "winning_months": round(sum(x > 0 for x in mo) / len(mo), 3),
            "worst_month": round(min(mo), 3), "best_month": round(max(mo), 3), "longest_losing_month_streak": streak,
            "by_year": {y: round(math.prod(1 + x for x in v) - 1, 3) for y, v in sorted(years.items())},
            "by_month": {m: round(v, 4) for m, v in zip(sorted(months), mo)},
        }
    # R by relative-volume bucket and side, no slippage
    def bucket(lo, hi):
        xs = [(1 if t["side"] == "long" else -1) * (t["exit"] - t["entry"]) / t["stop_d"] for t in trades if lo <= t["rv"] < hi]
        return {"n": len(xs), "avg_R": round(st.mean(xs), 3) if xs else None}
    out["by_rvol"] = {f"{lo}-{hi}": bucket(lo, hi) for lo, hi in ((1, 2), (2, 5), (5, 10), (10, 30), (30, 1e9))}
    out["by_side"] = {s: {"n": len(xs), "avg_R": round(st.mean(xs), 3) if xs else None}
                      for s in ("long", "short")
                      for xs in [[(1 if t["side"] == "long" else -1) * (t["exit"] - t["entry"]) / t["stop_d"] for t in trades if t["side"] == s]]}
    out["avg_stop_dollars"] = round(st.mean(t["stop_d"] for t in trades), 3)
    out["median_stop_dollars"] = round(st.median(t["stop_d"] for t in trades), 3)
    return out


def main():
    global universe_index
    universe = cache("universe", phase_universe)
    universe_index = {s: i for i, s in enumerate(universe)}
    if (OUT / "atr.npy").exists():
        days, atr = json.load(open(OUT / "days.json")), np.load(OUT / "atr.npy")
    else:
        days, atr = phase_daily(universe)
        json.dump(days, open(OUT / "days.json", "w")); np.save(OUT / "atr.npy", atr)
    log("days", len(days), "symbols ever eligible", int((~np.isnan(atr)).any(axis=1).sum()))
    picks = phase_select(universe, days, atr)
    log("days with picks", sum(1 for v in picks.values() if v))
    trades = cache("trades", lambda: phase_sim(picks))
    log("trades", len(trades))
    summ = summarize(trades, [d for d in days if picks.get(d)])
    json.dump(summ, open(OUT / "summary.json", "w"), indent=1)
    log("done", json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ("by_month",)} for k, v in summ.items() if k.startswith("slip")}))


if __name__ == "__main__":
    main()

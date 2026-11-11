#!/usr/bin/env python3
"""Out-of-sample test of two Stockbee swing setups (as defined in tradermonty/claude-trading-skills) on Jan 2024 - Nov 2026.

Universe: active US common stocks on Alpaca (ETFs removed by name), daily split-adjusted SIP bars.
Eligible on day t: close >= $5, volume >= 100k, 20-day average dollar volume >= $10M.

MB  (momentum burst, stockbee-momentum-burst-screener): close/prev close >= 1.04, volume > previous day.
    MB_q adds the screener's quality filters: close location >= 70%, prior-day gain <= 2%, no 3+ day up-streak
    into the trigger, no -4% day in the prior 5, prior 10-day base width <= 15%, risk to the trigger-day low <= 10%.
    Stop = trigger-day low. Exit: stop, else the close after 3 or 5 sessions.
EP  (episodic pivot, stockbee-episodic-pivot-analyzer, catalyst unknown so price/volume only): open gap >= 4% (EP)
    or >= 8% (EP8) over the prior close, volume >= 3x its 20-day average, close >= open, close location >= 50%.
    Stop = day-1 low. Exit: stop, else the close after 10 or 20 sessions.

Entries: "close" = the trigger-day close (needs a scan just before the close); "open" = next day's open (EOD scan).
A stop is checked from the first full day after entry; a gap through the stop exits at the open.
Costs: 0.05% each way (0.10% round trip), stressed at 0.25% each way.
Baseline: the same holding period from the same entry for every eligible stock that day (market drift).
Writes research/swing_oos/summary.json and trades.csv.gz. Survivorship: delisted names are missing (flatters longs).
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import requests

OUT = Path(__file__).resolve().parent / "swing_oos"
OUT.mkdir(exist_ok=True)
DATA = "https://data.alpaca.markets"
WARM, START, END = "2023-10-01", "2024-01-02", os.environ.get("SWING_END", "2026-11-10")
S = requests.Session()
S.headers.update({"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
ETF_WORDS = ("ETF", " FUND", "TRUST", "ISHARES", "SPDR", "PROSHARES", "DIREXION", "INVESCO", "VANGUARD", "GRANITESHARES",
             "WISDOMTREE", "GLOBAL X", "ULTRA", " 2X", " 3X", "LEVERAGED", "INVERSE", "NOTES", "WARRANT", "RIGHTS", " UNIT")
COSTS = (0.0005, 0.0025)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def get(path, params):
    for i in range(8):
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


def bars(symbols, start, end):
    out, params = {}, {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                       "feed": "sip", "limit": 10000, "adjustment": "split"}
    while True:
        j = get("/v2/stocks/bars", params)
        for s, bs in (j.get("bars") or {}).items():
            out.setdefault(s, []).extend(bs)
        if not j.get("next_page_token"):
            return out
        params["page_token"] = j["next_page_token"]


def universe():
    r = requests.get("https://paper-api.alpaca.markets/v2/assets", headers=S.headers,
                     params={"status": "active", "asset_class": "us_equity"}, timeout=60).json()
    keep = []
    for a in r:
        if a.get("exchange") not in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
            continue
        name, sym = (a.get("name") or "").upper(), a["symbol"]
        if any(w in name for w in ETF_WORDS) or "." in sym or "/" in sym or len(sym) > 5:
            continue
        keep.append(sym)
    return sorted(keep)


def load():
    f = OUT / "daily.npz"
    if f.exists():
        z = np.load(f, allow_pickle=True)
        return list(z["syms"]), list(z["days"]), z["O"], z["H"], z["L"], z["C"], z["V"]
    syms = universe()
    log("universe", len(syms))
    days = [x["t"][:10] for x in bars(["SPY"], WARM, END)["SPY"]]
    di = {d: i for i, d in enumerate(days)}
    shape = (len(syms), len(days))
    O, H, L, C, V = (np.full(shape, np.nan, dtype=np.float32) for _ in range(5))
    si = {s: i for i, s in enumerate(syms)}
    batches = [syms[i:i + 100] for i in range(0, len(syms), 100)]

    def work(b):
        return bars(b, WARM, END)

    with ThreadPoolExecutor(4) as ex:
        for n, res in enumerate(ex.map(work, batches)):
            for s, bs in res.items():
                i = si[s]
                for x in bs:
                    j = di.get(x["t"][:10])
                    if j is not None:
                        O[i, j], H[i, j], L[i, j], C[i, j], V[i, j] = x["o"], x["h"], x["l"], x["c"], x["v"]
            if n % 10 == 0:
                log("daily batch", n, "/", len(batches))
    np.savez_compressed(f, syms=np.array(syms), days=np.array(days), O=O, H=H, L=L, C=C, V=V)
    return syms, days, O, H, L, C, V


def shift(a, k):
    """a shifted right by k days (value from k days earlier), NaN-padded."""
    out = np.full_like(a, np.nan)
    out[:, k:] = a[:, :-k]
    return out


def rolling_mean_prev(a, n):
    """Mean of the n days before t (excludes t), float32, built from shifted copies to keep memory low."""
    s = np.zeros_like(a)
    c = np.zeros_like(a)
    for k in range(1, n + 1):
        sh = shift(a, k)
        ok = ~np.isnan(sh)
        s += np.where(ok, sh, 0)
        c += ok
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c >= n * 0.8, s / c, np.nan).astype(np.float32)


def rolling_ext_prev(a, n, fn):
    out = np.full_like(a, np.nan)
    for k in range(1, n + 1):
        sh = shift(a, k)
        out = np.where(np.isnan(out), sh, fn(out, sh)) if k > 1 else sh
    return out


def simulate(sig, entry_mode, hold, O, H, L, C, days, start_j):
    """sig: list of (i, t). Returns per-trade dicts with gross % and R."""
    T = len(days)
    out = []
    for i, t in sig:
        if t < start_j:
            continue
        stop = float(L[i, t])
        if entry_mode == "close":
            e_j, entry = t, float(C[i, t])
        else:
            e_j = t + 1
            if e_j >= T or np.isnan(O[i, e_j]):
                continue
            entry = float(O[i, e_j])
        if not (entry > stop > 0):
            continue
        risk = entry - stop
        exit_px, how, x_j = None, "time", None
        last = min(T - 1, e_j + hold)
        if last <= e_j:
            continue
        # entry day itself (open entries): the stop counts after the open
        if entry_mode == "open" and L[i, e_j] <= stop:
            exit_px, how, x_j = stop, "stop", e_j
        j = e_j + 1
        while exit_px is None and j <= last:
            if np.isnan(O[i, j]):
                j += 1
                continue
            if O[i, j] <= stop:
                exit_px, how, x_j = float(O[i, j]), "stop_gap", j
            elif L[i, j] <= stop:
                exit_px, how, x_j = stop, "stop", j
            j += 1
        if exit_px is None:
            k = last
            while k > e_j and np.isnan(C[i, k]):
                k -= 1
            if k <= e_j and entry_mode == "open":
                continue
            exit_px, x_j = float(C[i, k]), k
        out.append({"i": i, "t": t, "day": days[t], "entry": entry, "stop": stop, "exit": exit_px, "how": how,
                    "risk_pct": risk / entry, "gross": exit_px / entry - 1, "R": (exit_px - entry) / risk,
                    "e_j": e_j, "x_j": x_j})
    return out


def stats(trades, cost):
    if not trades:
        return {"n": 0}
    net = np.array([t["gross"] - 2 * cost for t in trades])
    R = np.array([(t["gross"] - 2 * cost) / t["risk_pct"] for t in trades])
    wins = net > 0
    by_year = defaultdict(list)
    by_month = defaultdict(list)
    for t, r in zip(trades, R):
        by_year[t["day"][:4]].append(r)
        by_month[t["day"][:7]].append(r)
    pf = net[wins].sum() / -net[~wins].sum() if (~wins).any() and net[~wins].sum() < 0 else None
    # cap each trade's R contribution at the 99th percentile to show dependence on outliers
    top10 = np.sort(R)[-10:].sum() if len(R) >= 10 else R.sum()
    return {"n": len(trades), "win": round(float(wins.mean()), 3), "avg_pct": round(float(net.mean()) * 100, 3),
            "med_pct": round(float(np.median(net)) * 100, 3), "avg_R": round(float(R.mean()), 3),
            "sum_R": round(float(R.sum()), 1), "sum_R_ex_top10": round(float(R.sum() - top10), 1),
            "profit_factor": round(float(pf), 2) if pf else None,
            "avg_risk_pct": round(float(np.mean([t["risk_pct"] for t in trades])) * 100, 2),
            "by_year_avg_R": {y: round(float(np.mean(v)), 3) for y, v in sorted(by_year.items())},
            "by_year_n": {y: len(v) for y, v in sorted(by_year.items())},
            "pos_months": round(float(np.mean([np.mean(v) > 0 for v in by_month.values()])), 2),
            "months": len(by_month)}


def baseline(trades, entry_mode, hold, O, C, elig):
    """Average return of every eligible stock over the same entry->exit window as each trade's own day."""
    T = C.shape[1]
    cache = {}
    vals = []
    for t in trades:
        key = (t["t"], t["x_j"])
        if key not in cache:
            tt, xj = t["t"], t["x_j"]
            e = C[:, tt] if entry_mode == "close" else (O[:, tt + 1] if tt + 1 < T else None)
            x = C[:, xj]
            m = elig[:, tt] & ~np.isnan(e) & ~np.isnan(x) & (e > 0)
            cache[key] = float(np.mean(x[m] / e[m] - 1)) if m.any() else 0.0
        vals.append(cache[key])
    return round(float(np.mean(vals)) * 100, 3) if vals else None


def main():
    syms, days, O, H, L, C, V = load()
    keep = ~np.all(np.isnan(C), axis=1)
    syms = [s for s, k in zip(syms, keep) if k]
    O, H, L, C, V = O[keep], H[keep], L[keep], C[keep], V[keep]
    start_j = next(j for j, d in enumerate(days) if d >= START)
    log("matrix", O.shape, "start index", start_j)
    pc = shift(C, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        gain = C / pc - 1
        prev_gain = shift(gain, 1)
        rng = H - L
        cloc = np.where(rng > 0, (C - L) / rng, 0.0)
        gap = O / pc - 1
        dv20 = rolling_mean_prev(C * V, 20)
        v20 = rolling_mean_prev(V, 20)
        elig = (C >= 5) & (V >= 100_000) & (dv20 >= 10_000_000)
        up = (gain > 0).astype(np.int8)
        streak3 = (shift(up.astype(np.float32), 1) + shift(up.astype(np.float32), 2) + shift(up.astype(np.float32), 3)) >= 3
        min_prior5 = rolling_ext_prev(gain, 5, np.fmin)
        hi10 = rolling_ext_prev(H, 10, np.fmax)
        lo10 = rolling_ext_prev(L, 10, np.fmin)
        base_w = hi10 / lo10 - 1
        risk_close = (C - L) / C
        mb = elig & (gain >= 0.04) & (V > shift(V, 1))
        mb_q = mb & (cloc >= 0.70) & (prev_gain <= 0.02) & ~streak3 & ~(min_prior5 <= -0.04) & (base_w <= 0.15) & (risk_close <= 0.10)
        ep_core = elig & (V >= 3 * v20) & (C >= O) & (cloc >= 0.50)
        ep = ep_core & (gap >= 0.04)
        ep8 = ep_core & (gap >= 0.08)
    del up, pc, gain, prev_gain, rng, cloc, gap, dv20, streak3, min_prior5, hi10, lo10, base_w, risk_close, ep_core
    setups = {"MB": (mb, (3, 5)), "MB_q": (mb_q, (3, 5)), "EP": (ep, (10, 20)), "EP8": (ep8, (10, 20))}
    summary = {"window": [START, END], "universe": len(syms), "days": len(days) - start_j, "costs_each_way": COSTS,
               "notes": __doc__.strip().splitlines()[0]}
    rows = []
    for name, (mask, holds) in setups.items():
        ii, tt = np.nonzero(mask)
        sig = sorted(zip(ii.tolist(), tt.tolist()), key=lambda x: x[1])
        log(name, "signals", len(sig))
        for mode in ("close", "open"):
            for hold in holds:
                tr = simulate(sig, mode, hold, O, H, L, C, days, start_j)
                key = f"{name}|{mode}|{hold}d"
                summary[key] = {f"cost_{c}": stats(tr, c) for c in COSTS}
                summary[key]["baseline_avg_pct"] = baseline(tr, mode, hold, O, C, elig)
                # realistic capacity: the 2 highest-volume-ratio signals per day only
                byday = defaultdict(list)
                for t in tr:
                    byday[t["t"]].append(t)
                top2 = [x for d in byday.values()
                        for x in sorted(d, key=lambda t: -(V[t["i"], t["t"]] / max(v20[t["i"], t["t"]], 1)))[:2]]
                summary[key]["top2_per_day"] = stats(top2, COSTS[0])
                s0 = summary[key][f"cost_{COSTS[0]}"]
                log(key, "n", s0.get("n"), "avgR", s0.get("avg_R"), "avg%", s0.get("avg_pct"),
                    "base%", summary[key]["baseline_avg_pct"], "top2 avgR", summary[key]["top2_per_day"].get("avg_R"))
                if mode == "open":
                    for t in tr:
                        rows.append([name, hold, t["day"], syms[t["i"]], round(t["entry"], 4), round(t["stop"], 4),
                                     round(t["exit"], 4), t["how"], round(t["R"], 3), round(t["gross"] * 100, 3)])
        json.dump(summary, open(OUT / "summary.json", "w"), indent=1)
    with gzip.open(OUT / "trades.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["setup", "hold", "day", "symbol", "entry", "stop", "exit", "how", "R", "gross_pct"])
        w.writerows(rows)
    log("done")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Replay the desk's logged plays with full-market (SIP) 1-minute bars, SIP trades at the
crossing minute, and OPRA 1-minute option bars, under the live monitor's rules.

Usage (on the server, as deskmon, with monitor/.env loaded):
  python3 research/replay.py research/plays.json research/radar.json research/replay-out.json

What it answers, per play:
- when the trigger level was actually first crossed (to the second, by an eligible round-lot trade)
- whether the monitor's confirmation (current rules) passes at that second, or at a 5-minute re-check
- the option price at that moment and the outcome under the playbook's exits
It ignores position limits and liquidity passes (those were decided at selection) and marks them.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "monitor"))
import desk_core as c  # noqa: E402
from desk_core import ET, Item  # noqa: E402

DATA = "https://data.alpaca.markets"
S = requests.Session()
S.headers.update({"APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
LAST_DAY = date(2026, 11, 6)
SLIP = 0.25
CACHE: dict = {}


def get(url, params):
    for i in range(5):
        r = S.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 + i)
            continue
        if r.status_code >= 400:
            return {"_error": r.status_code, "_text": r.text[:200]}
        return r.json()
    return {"_error": 429}


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stock_bars(sym: str, d: date) -> list[dict]:
    """1-minute SIP bars for d, plus the prior ~3 weeks (for the volume profile), cached per symbol."""
    key = ("sb", sym)
    if key not in CACHE:
        CACHE[key] = {}
        start = datetime(2026, 9, 25, tzinfo=ET)
        end = datetime(2026, 11, 7, tzinfo=ET)
        params = {"timeframe": "1Min", "start": utc(start), "end": utc(end), "feed": "sip", "limit": 10000, "adjustment": "raw"}
        bars = []
        while True:
            j = get(f"{DATA}/v2/stocks/{sym}/bars", params)
            bars += j.get("bars") or []
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
        for b in bars:
            CACHE[key].setdefault(c.to_et(b["t"]).date(), []).append(b)
    return CACHE[key].get(d, [])


def all_bars_before(sym: str, d: date) -> list[dict]:
    stock_bars(sym, d)
    out = []
    for dd, bs in CACHE[("sb", sym)].items():
        if dd < d:
            out += bs
    return out


def prev_close(sym: str, d: date) -> float | None:
    stock_bars(sym, d)
    days = sorted(dd for dd in CACHE[("sb", sym)] if dd < d)
    for dd in reversed(days):
        rth = [b for b in CACHE[("sb", sym)][dd] if c.in_rth(c.to_et(b["t"]))]
        if rth:
            return rth[-1]["c"]
    return None


def first_trade_cross(sym: str, minute: datetime, direction: str, level: float) -> tuple[datetime, float] | None:
    j = get(f"{DATA}/v2/stocks/{sym}/trades", {"start": utc(minute), "end": utc(minute + timedelta(minutes=1)),
                                                "feed": "sip", "limit": 10000})
    for t in j.get("trades") or []:
        if c.trade_crosses(direction, level, float(t["p"]), int(t.get("s", 0)), t.get("c")):
            return c.to_et(t["t"]), float(t["p"])
    return None


def option_bars(occ: str, d0: date, d1: date) -> list[dict]:
    key = ("ob", occ, d0, d1)
    if key not in CACHE:
        params = {"symbols": occ, "timeframe": "1Min", "start": utc(datetime.combine(d0, datetime.min.time(), ET)),
                  "end": utc(datetime.combine(d1 + timedelta(days=1), datetime.min.time(), ET)), "limit": 10000}
        bars = []
        while True:
            j = get(f"{DATA}/v1beta1/options/bars", params)
            bars += (j.get("bars") or {}).get(occ, [])
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
        CACHE[key] = bars
    return CACHE[key]


def occ(root: str, exp: date, typ: str, strike: float) -> str:
    return f"{root}{exp:%y%m%d}{'C' if typ.startswith('c') else 'P'}{int(round(strike * 1000)):08d}"


def find_contract(p: dict, at: datetime, price: float) -> tuple[str | None, str]:
    k = p.get("contract") or {}
    typ = p["direction"]
    if k.get("strike") and k.get("expiration"):
        sym = occ(p["underlying"], date.fromisoformat(k["expiration"]), typ, float(k["strike"]))
        if any(c.to_et(b["t"]) <= at for b in option_bars(sym, at.date(), at.date())):
            return sym, "play contract"
    # scalps with no chosen contract: nearest-strike weekly that actually traded
    d = at.date()
    fri = d + timedelta(days=(4 - d.weekday()) % 7)
    for exp in (d, fri, fri + timedelta(days=7)):
        for inc in (1, 2.5, 5, 0.5, 10):
            strike = round(price / inc) * inc
            sym = occ(p["underlying"], exp, typ, strike)
            if any(c.to_et(b["t"]) <= at + timedelta(minutes=5) for b in option_bars(sym, d, d)):
                return sym, f"nearest strike {strike:g}, exp {exp:%m/%d}"
    return None, "no traded contract found"


def opt_price_at(bars: list[dict], at: datetime) -> tuple[float, datetime] | None:
    """Price of the option at `at`: VWAP of the 1-minute bar containing it, else the last bar before."""
    best = None
    for b in bars:
        t = c.to_et(b["t"])
        if t <= at:
            best = b
        else:
            break
    if not best:
        return None
    return float(best.get("vw") or best["c"]), c.to_et(best["t"])


def sessions(d0: date, d1: date):
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def radar_lists(radar: dict, d: date) -> tuple[list, list]:
    prior = [k for k in radar if k < d.isoformat()]
    if not prior:
        return [], []
    r = radar[max(prior)]
    return r.get("leaders") or [], r.get("laggards") or []


def stock_leg(p, sym, d, at, px, want_dir, swing, level):
    """Same entry in shares: stock stop/target from the plan (scalps without one: the trigger level's far side,
    1% of price), 2R target when none, 15:05 close for scalps, 10 sessions for swings. 1 cent slippage each way."""
    planned = p.get("planned") or {}
    sign = 1 if want_dir == "above" else -1
    entry = px + sign * 0.01
    stop = planned.get("stock_stop")
    if not stop or (stop - entry) * sign >= 0:
        stop = entry - sign * entry * (0.01 if not swing else 0.03)
    risk = abs(entry - stop)
    tgt = planned.get("target_stock")
    if not tgt or (tgt - entry) * sign <= 0:
        tgt = entry + sign * 2 * risk
    end_day = d if not swing else LAST_DAY
    seen_days = set()
    for dd in sessions(d, end_day):
        for x in stock_bars(sym, dd):
            t = c.to_et(x["t"])
            if not c.in_rth(t) or t <= at:
                continue
            seen_days.add(dd)
            if not swing and t.time() >= datetime.strptime("15:05", "%H:%M").time():
                ex = x["o"]; how = "15:05 close"
            elif (sign > 0 and x["l"] <= stop) or (sign < 0 and x["h"] >= stop):
                ex = min(stop, x["o"]) if sign > 0 else max(stop, x["o"]); how = "stop"
            elif (sign > 0 and x["h"] >= tgt) or (sign < 0 and x["l"] <= tgt):
                ex = max(tgt, x["o"]) if sign > 0 else min(tgt, x["o"]); how = "target"
            elif swing and len(seen_days) > 10:
                ex = x["c"]; how = "10-session time stop"
            else:
                continue
            ex -= sign * 0.01
            return {"entry": round(entry, 3), "stop": round(stop, 3), "target": round(tgt, 3), "exit": round(ex, 3),
                    "how": how, "exit_time": t.isoformat(), "r": round(sign * (ex - entry) / risk, 2),
                    "pct": round(sign * (ex - entry) / entry, 4)}
    return {"entry": round(entry, 3), "how": "no exit in data"}


def replay(p: dict, radar: dict, cancelled_pairs: set) -> dict:
    out = {"id": p["id"], "module": p["module"], "setup": p.get("setup"), "direction": p["direction"],
           "underlying": p["underlying"], "desk_status": p.get("status"),
           "desk_pass_reason": (p.get("pass_reason") or "")[:120], "desk_pnl": p.get("pnl"), "desk_r": p.get("r_multiple")}
    trig = p.get("trigger") or {}
    created = c.to_et(p["created_at"])
    first_day = created.date() if c.in_rth(created) or created.time() < c.RTH_OPEN else created.date() + timedelta(days=1)
    last_day = min(date.fromisoformat(p.get("expires_at") or created.date().isoformat()), LAST_DAY)
    sym = p["underlying"]
    want_dir = "above" if p["direction"] == "call" else "below"
    swing = p["module"] == "swing"
    conf = dict(p.get("confirmation") or {})
    conf.update({"spy_block_pct": 0.0075, "skip_first_minutes": 5})
    rvol_req = conf.get("rvol_min")
    if os.environ.get("REPLAY_IGNORE_RVOL") == "1":
        conf.pop("rvol_min", None)
    if not swing:
        conf["max_extension_pct"] = conf.get("max_extension_pct", 0.01)
    item_raw = {"symbol": sym, "kind": "entry", "direction": p["direction"], "trigger": {"type": want_dir, "price": trig.get("price")},
                "confirmation": conf}
    for d in sessions(first_day, last_day):
        bars = [b for b in stock_bars(sym, d) if c.in_rth(c.to_et(b["t"]))]
        if not bars:
            continue
        level = trig.get("price")
        if p.get("setup") == "orb":  # rebuild the opening range at full-market precision
            r = c.opening_range(bars, d, 30)
            if not r:
                continue
            level = r[0] if want_dir == "above" else r[1]
            after = c.session_open(d) + timedelta(minutes=30)
        else:
            after = created if created.date() == d and c.in_rth(created) else None
        if not level:
            out["result"] = "no trigger level"
            return out
        level = float(level)
        b = c.first_bar_cross(want_dir, level, bars, after)
        if not b:
            continue
        minute = c.to_et(b["t"])
        tc = first_trade_cross(sym, minute, want_dir, level)
        at, px = tc if tc else (minute + timedelta(seconds=59), b["c"])
        out.update({"crossed_at": at.isoformat(), "cross_price": px, "level": level, "cross_source": "trade" if tc else "bar"})
        # confirmation now, then every 5 minutes while beyond the level (monitor behaviour), max 8 tries
        leaders, laggards = radar_lists(radar, d)
        sector_ok = (p["direction"] == "call" and p.get("sector_etf") in leaders) or (p["direction"] == "put" and p.get("sector_etf") in laggards)
        if sector_ok:
            item_raw["confirmation"]["rs_exempt"] = {"min_move_pct": 0.01, "hard_block_pct": 0.015}
        item = Item(item_raw)
        prof = c.same_time_profile(all_bars_before(sym, d), d, 10)
        spy_bars = [x for x in stock_bars("SPY", d) if c.in_rth(c.to_et(x["t"]))]
        spy_pc, und_pc = prev_close("SPY", d), prev_close(sym, d)
        checks_log = []
        confirmed_at = None
        t = at
        for i in range(8):
            upto = [x for x in bars if c.to_et(x["t"]) <= t]
            if not upto:
                break
            price = px if i == 0 else upto[-1]["c"]
            beyond = price >= level if want_dir == "above" else price <= level
            if i and not beyond:
                t += timedelta(minutes=5)
                continue
            stats = c.session_stats(bars, d, upto=t)
            spy_now = [x for x in spy_bars if c.to_et(x["t"]) <= t]
            spy_chg = spy_now[-1]["c"] / spy_pc - 1 if spy_now and spy_pc else None
            und_chg = price / und_pc - 1 if und_pc else None
            checks = c.confirm_entry(item, want_dir, level, price, t, stats, None, spy_chg, rvol_profile=prof, underlying_change=und_chg)
            rv = c.rvol_same_time(stats.volume, t, prof)
            checks_log.append({"time": t.isoformat(), "price": price, "rvol": round(rv, 2) if rv else None,
                               "failed": [f"{x.name}: {x.note}" for x in checks if not x.ok]})
            if all(x.ok for x in checks):
                confirmed_at, entry_px = t, price
                break
            t += timedelta(minutes=5)
            if t.time() >= datetime.strptime("15:55", "%H:%M").time():
                break
        out["checks"] = checks_log
        pair_key = f"{sym}-{d}"
        if not confirmed_at:
            out["result"] = "monitor would pass"
            return out
        if not swing and pair_key in cancelled_pairs:
            out["result"] = "twin already taken"
            return out
        cancelled_pairs.add(pair_key)
        out["confirmed_at"] = confirmed_at.isoformat()
        out["stock"] = stock_leg(p, sym, d, confirmed_at, entry_px, want_dir, swing, level)
        out["rvol_at_entry"] = checks_log[-1].get("rvol")
        out["rvol_required"] = rvol_req
        out["confirm_delay_min"] = round((confirmed_at - at).total_seconds() / 60, 1)
        osym, how = find_contract(p, confirmed_at, entry_px)
        if not osym:
            out["result"] = "confirmed, no option data"
            return out
        end_day = min(LAST_DAY, last_day if not swing else LAST_DAY)
        if not swing:
            end_day = d
        ob = [x for x in option_bars(osym, d, end_day) if c.in_rth(c.to_et(x["t"]))]
        op = opt_price_at(ob, confirmed_at)
        if not op:
            out["result"] = "confirmed, no option print yet"
            out["option"] = osym
            return out
        spread_pct = (p.get("contract") or {}).get("spread_pct") or (0.05 if swing else 0.03)
        fill = op[0] * (1 + SLIP * spread_pct)
        stop_pct = 0.45 if swing else 0.28
        tgt_mult = 1.8 if swing else 1.40
        opt_stop, opt_tgt = fill * (1 - stop_pct), fill * tgt_mult
        planned = p.get("planned") or {}
        stk_stop, stk_tgt = planned.get("stock_stop"), planned.get("target_stock")
        out.update({"option": osym, "option_how": how, "entry_fill": round(fill, 3), "entry_bar_time": op[1].isoformat()})
        # walk forward minute by minute
        exit_ = None
        sbars = {}
        for dd in sessions(d, end_day):
            for x in stock_bars(sym, dd):
                if c.in_rth(c.to_et(x["t"])):
                    sbars[c.to_et(x["t"])] = x
        be_raised = False
        for x in ob:
            tt = c.to_et(x["t"])
            if tt <= confirmed_at:
                continue
            sb = sbars.get(tt)
            if not swing and tt.time() >= datetime.strptime("15:05", "%H:%M").time():
                exit_ = (tt, x["o"] * (1 - SLIP * spread_pct), "15:05 scalp close")
                break
            if x["l"] <= opt_stop:
                exit_ = (tt, min(opt_stop, x["o"]) * (1 - SLIP * spread_pct), "option stop")
                break
            if sb and stk_stop and ((want_dir == "above" and sb["l"] <= stk_stop) or (want_dir == "below" and sb["h"] >= stk_stop)):
                exit_ = (tt, x["c"] * (1 - SLIP * spread_pct), "stock stop")
                break
            if x["h"] >= opt_tgt:
                exit_ = (tt, max(opt_tgt, x["o"]) * (1 - SLIP * spread_pct), "option target")
                break
            if sb and stk_tgt and ((want_dir == "above" and sb["h"] >= stk_tgt) or (want_dir == "below" and sb["l"] <= stk_tgt)):
                exit_ = (tt, x["c"] * (1 - SLIP * spread_pct), "stock target")
                break
            if swing and not be_raised and x["h"] >= fill * 1.5:
                opt_stop, be_raised = fill, True
            if swing and len({k.date() for k in sbars if confirmed_at <= k <= tt}) > 10:
                exit_ = (tt, x["c"] * (1 - SLIP * spread_pct), "10-session time stop")
                break
        if not exit_ and ob:
            last = ob[-1]
            exit_ = (c.to_et(last["t"]), last["c"] * (1 - SLIP * spread_pct), "open at end of data (marked)")
        if exit_:
            pct = exit_[1] / fill - 1
            out.update({"exit_time": exit_[0].isoformat(), "exit_fill": round(exit_[1], 3), "exit_reason": exit_[2],
                        "pnl_pct": round(pct, 4), "r": round(pct / stop_pct, 2), "result": "trade"})
        return out
    out["result"] = "never crossed"
    return out


def main():
    plays = json.load(open(sys.argv[1]))
    radar = json.load(open(sys.argv[2]))
    plays.sort(key=lambda p: (p["created_at"], 0 if p.get("setup") != "orb" else 1))
    taken: set = set()
    res = []
    for i, p in enumerate(plays):
        try:
            r = replay(p, radar, taken)
        except Exception as e:  # keep going
            r = {"id": p.get("id"), "result": f"error: {e}"}
        res.append(r)
        print(f"{i+1}/{len(plays)} {r['id']}: {r.get('result')}", flush=True)
    json.dump(res, open(sys.argv[3], "w"), indent=1)


if __name__ == "__main__":
    main()

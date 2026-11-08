"""Pure logic for the Rotation Desk monitor: no network, no disk.

Everything here is deterministic and unit-tested in test_core.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# SIP sale conditions that should not set a trigger (not "last sale eligible",
# out of sequence, average-price, extended-hours or derived prints).
EXCLUDED_CONDITIONS = {"B", "C", "G", "H", "I", "M", "N", "P", "Q", "R", "T", "U", "V", "W", "Z", "4", "7", "9"}
MIN_TRIGGER_SIZE = 100  # ignore odd-lot prints for the trigger itself

ENTRY_KINDS = {"entry"}
STOCK_EXIT_KINDS = {"stop", "target"}
OPTION_EXIT_KINDS = {"option_stop", "option_target"}


# ---------------------------------------------------------------- time
def to_et(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(ET)
    s = ts.replace("Z", "+00:00")
    # Alpaca sends nanoseconds; Python handles at most microseconds
    if "." in s:
        head, rest = s.split(".", 1)
        n = 0
        while n < len(rest) and rest[n].isdigit():
            n += 1
        frac, tz = rest[:n], rest[n:]
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).astimezone(ET)


def in_rth(t: datetime) -> bool:
    t = t.astimezone(ET)
    return t.weekday() < 5 and RTH_OPEN <= t.time() < RTH_CLOSE


def session_open(d: date) -> datetime:
    return datetime.combine(d, RTH_OPEN, ET)


def session_fraction(t: datetime) -> float:
    mins = (t.astimezone(ET) - session_open(t.astimezone(ET).date())).total_seconds() / 60
    return min(max(mins, 1.0), 390.0) / 390.0


# ---------------------------------------------------------------- feed items
@dataclass
class Item:
    raw: dict

    @property
    def id(self) -> str:
        r = self.raw
        return r.get("id") or f"{r['symbol']}:{self.kind}:{(r.get('trigger') or {}).get('price')}"

    @property
    def symbol(self) -> str:
        return self.raw["symbol"].upper()

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "entry")

    @property
    def trigger(self) -> dict:
        return self.raw.get("trigger") or {}

    @property
    def ttype(self) -> str:
        return self.trigger.get("type", "")

    @property
    def level(self) -> float | None:
        p = self.trigger.get("price")
        return float(p) if p is not None else None

    @property
    def option_symbol(self) -> str | None:
        return self.raw.get("option_symbol")

    @property
    def armed_after(self) -> datetime | None:
        a = self.raw.get("armed_after")
        return to_et(a) if a else None


def active_items(feed: dict, today: date) -> list[Item]:
    out = []
    for r in feed.get("watch", []) or []:
        if r.get("status", "active") != "active":
            continue
        if r.get("expires") and r["expires"] < today.isoformat():
            continue
        if not r.get("symbol") or not r.get("trigger"):
            continue
        out.append(Item(r))
    return out


# ---------------------------------------------------------------- opening range
def opening_range(bars: list[dict], d: date, minutes: int) -> tuple[float, float] | None:
    """High/low of the first `minutes` of the session from 1-minute bars.
    Returns None until the range is complete."""
    start = session_open(d)
    end = start + timedelta(minutes=minutes)
    inside = [b for b in bars if start <= to_et(b["t"]) < end]
    if len(inside) < max(1, minutes // 2):  # need most of the window
        return None
    if not any(to_et(b["t"]) >= end - timedelta(minutes=1) for b in bars):
        return None
    return max(b["h"] for b in inside), min(b["l"] for b in inside)


def resolve_level(item: Item, orb: tuple[float, float] | None) -> tuple[str, float] | None:
    """Return (direction, level) for a stock-price item, resolving ORB items."""
    t = item.ttype
    if t in ("above", "below"):
        return (t, item.level) if item.level is not None else None
    if t in ("orb_high", "orb_low"):
        if not orb:
            return None
        return ("above", orb[0]) if t == "orb_high" else ("below", orb[1])
    return None


# ---------------------------------------------------------------- trigger detection
def trade_crosses(direction: str, level: float, price: float, size: int, conditions: list[str] | None) -> bool:
    if size < MIN_TRIGGER_SIZE:
        return False
    if conditions and any(c in EXCLUDED_CONDITIONS for c in conditions):
        return False
    return price >= level if direction == "above" else price <= level


def first_bar_cross(direction: str, level: float, bars: list[dict], after: datetime | None) -> dict | None:
    """First regular-session 1-minute bar at/after `after` that reached the level."""
    for b in bars:
        t = to_et(b["t"])
        if not in_rth(t):
            continue
        if after and t + timedelta(minutes=1) <= after:
            continue
        if (direction == "above" and b["h"] >= level) or (direction == "below" and b["l"] <= level):
            return b
    return None


# ---------------------------------------------------------------- session stats
@dataclass
class SessionStats:
    vwap: float | None
    volume: float
    high: float | None
    low: float | None


def session_stats(bars: list[dict], d: date, upto: datetime | None = None) -> SessionStats:
    pv = v = 0.0
    hi = lo = None
    for b in bars:
        t = to_et(b["t"])
        if t.date() != d or not in_rth(t):
            continue
        if upto and t > upto:
            continue
        vol = float(b.get("v") or 0)
        px = float(b.get("vw") or b.get("c"))
        pv += px * vol
        v += vol
        hi = b["h"] if hi is None else max(hi, b["h"])
        lo = b["l"] if lo is None else min(lo, b["l"])
    return SessionStats(vwap=(pv / v) if v else None, volume=v, high=hi, low=lo)


# ---------------------------------------------------------------- relative volume
def minute_of_session(t: datetime) -> int:
    t = t.astimezone(ET)
    return int((t - session_open(t.date())).total_seconds() // 60)


def same_time_profile(bars: list[dict], today: date, sessions: int = 10) -> dict[date, list[float]]:
    """Cumulative regular-session volume by minute for each of the prior `sessions` days."""
    days: dict[date, list[float]] = {}
    for b in bars:
        t = to_et(b["t"])
        if t.date() >= today or not in_rth(t):
            continue
        days.setdefault(t.date(), [0.0] * 390)[minute_of_session(t)] += float(b.get("v") or 0)
    keep = sorted(days)[-sessions:]
    out = {}
    for d in keep:
        cum, acc = [], 0.0
        for v in days[d]:
            acc += v
            cum.append(acc)
        out[d] = cum
    return out


def rvol_same_time(today_volume: float, at: datetime, profile: dict[date, list[float]]) -> float | None:
    """Section 8 method: today's volume to now / average volume to the same clock time over prior sessions."""
    if len(profile) < 5:
        return None
    m = min(max(minute_of_session(at), 0), 389)
    avg = sum(cum[m] for cum in profile.values()) / len(profile)
    return today_volume / avg if avg > 0 else None


# ---------------------------------------------------------------- confirmation
@dataclass
class Check:
    name: str
    ok: bool
    note: str


def confirm_entry(item: Item, direction: str, level: float, price: float, at: datetime,
                  stats: SessionStats, avg_daily_volume: float | None, spy_change: float | None,
                  rvol_profile: dict | None = None, underlying_change: float | None = None) -> list[Check]:
    conf = item.raw.get("confirmation") or {}
    checks: list[Check] = []
    ext = abs(price - level) / level
    if conf.get("max_extension_pct") is not None:
        m = float(conf["max_extension_pct"])
        checks.append(Check("extension", ext <= m, f"{ext*100:.2f}% past trigger (max {m*100:.1f}%)"))
    want = conf.get("vwap")
    if want in ("above", "below") and stats.vwap:
        ok = price >= stats.vwap if want == "above" else price <= stats.vwap
        side = "above" if price >= stats.vwap else "below"
        checks.append(Check("vwap", ok, f"{side} VWAP {stats.vwap:.2f}"))
    if conf.get("rvol_min") is not None:
        rmin = float(conf["rvol_min"])
        rvol = rvol_same_time(stats.volume, at, rvol_profile) if rvol_profile else None
        method = "1m-same-time-10d"
        if rvol is None and avg_daily_volume:
            rvol, method = stats.volume / (avg_daily_volume * session_fraction(at)), "daily-frac"
        if rvol is not None:
            checks.append(Check("rvol", rvol >= rmin, f"volume {rvol:.2f}x normal for this time (min {rmin:.2f}x, {method})"))
    lim = conf.get("spy_block_pct")
    if lim is not None and spy_change is not None:
        lim = float(lim)
        against = -spy_change if direction == "above" else spy_change
        blocked = against >= lim
        note = f"SPY {spy_change*100:+.2f}% today (blocks at {lim*100:.2f}%)"
        ex = conf.get("rs_exempt")  # set by the desk only when the play's group is a radar leader/laggard
        if blocked and ex and underlying_change is not None:
            hard = float(ex.get("hard_block_pct", 0.015))
            need = float(ex.get("min_move_pct", 0.01))
            with_trade = underlying_change if direction == "above" else -underlying_change
            if against < hard and with_trade >= need:
                blocked = False
                note += f"; RS exemption: {item.symbol} {underlying_change*100:+.2f}% with the trade"
        checks.append(Check("market", not blocked, note))
    open_skip = int(conf.get("skip_first_minutes", 15))
    if open_skip and at < session_open(at.date()) + timedelta(minutes=open_skip):
        checks.append(Check("open", False, f"inside the first {open_skip} minutes"))
    return checks


# ---------------------------------------------------------------- options
def parse_occ(sym: str) -> dict:
    """XLE261219C00096000 -> underlying, expiration, type, strike."""
    root = sym[:-15]
    y, m, d = int(sym[-15:-13]), int(sym[-13:-11]), int(sym[-11:-9])
    return {"underlying": root, "expiration": date(2000 + y, m, d), "type": "call" if sym[-9] == "C" else "put",
            "strike": int(sym[-8:]) / 1000}


def occ_label(sym: str) -> str:
    p = parse_occ(sym)
    strike = f"{p['strike']:g}"
    return f"{p['underlying']} {p['expiration']:%m/%d} ${strike}{'C' if p['type']=='call' else 'P'}"


def quote_from_snapshot(snap: dict) -> dict | None:
    q = (snap or {}).get("latestQuote") or {}
    bid, ask = q.get("bp"), q.get("ap")
    if not bid or not ask or ask <= 0 or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    g = snap.get("greeks") or {}
    return {"bid": bid, "ask": ask, "mid": round(mid, 4), "spread_pct": round((ask - bid) / mid, 4),
            "delta": g.get("delta"), "iv": snap.get("impliedVolatility"), "quote_time": q.get("t")}


def est_fill(q: dict, slippage_frac: float, side: str) -> float:
    slip = slippage_frac * (q["ask"] - q["bid"])
    px = q["mid"] + slip if side == "buy" else q["mid"] - slip
    return round(max(px, 0.01), 2)


def pick_contract(chain: dict, direction: str, rule: dict, today: date) -> tuple[str, dict] | None:
    """Choose a contract from an options chain snapshot using the play's rule:
    {"dte": [min,max], "delta": [lo,hi], "max_spread_pct": x, "max_premium": dollars_per_contract}."""
    want = "call" if direction in ("call", "above") else "put"
    dlo, dhi = rule.get("delta", [0.4, 0.6])
    tmin, tmax = rule.get("dte", [0, 7])
    target = (dlo + dhi) / 2
    best = None
    for sym, snap in (chain or {}).items():
        try:
            p = parse_occ(sym)
        except (ValueError, IndexError):
            continue
        if p["type"] != want:
            continue
        dte = (p["expiration"] - today).days
        if not (tmin <= dte <= tmax):
            continue
        q = quote_from_snapshot(snap)
        if not q or q["delta"] is None:
            continue
        d = abs(q["delta"])
        if not (dlo <= d <= dhi):
            continue
        if rule.get("max_spread_pct") is not None and q["spread_pct"] > rule["max_spread_pct"]:
            continue
        if rule.get("max_premium") is not None and q["ask"] * 100 > rule["max_premium"]:
            continue
        score = (abs(d - target), dte)
        if best is None or score < best[0]:
            best = (score, sym, q)
    return (best[1], best[2]) if best else None


# ---------------------------------------------------------------- events + messages
def build_event(item: Item, *, kind: str, at: datetime, level: float, price: float, source: str,
                checks: list[Check] | None = None, stats: SessionStats | None = None,
                option_symbol: str | None = None, quote: dict | None = None, slippage_frac: float = 0.25,
                note: str = "", crossed_at: datetime | None = None) -> dict:
    side = "buy" if kind in ENTRY_KINDS else "sell"
    ev = {
        "id": f"{item.id}@{at:%Y%m%dT%H%M%S}",
        "item_id": item.id, "play_id": item.raw.get("play_id"), "kind": kind,
        "symbol": item.symbol, "direction": item.raw.get("direction"), "module": item.raw.get("module"),
        "setup": item.raw.get("setup"),
        "time": at.isoformat(), "trigger_level": round(level, 4), "price": round(price, 4), "source": source,
        "extension_pct": round(abs(price - level) / level, 5),
        "crossed_at": (crossed_at or at).isoformat(),
    }
    if checks is not None:
        ev["checks"] = [{"name": c.name, "ok": c.ok, "note": c.note} for c in checks]
        ev["confirmed"] = all(c.ok for c in checks)
    if stats:
        ev["session_vwap"] = round(stats.vwap, 4) if stats.vwap else None
        ev["session_volume"] = stats.volume
    if option_symbol:
        ev["option"] = {"symbol": option_symbol, "label": occ_label(option_symbol)}
        if quote:
            ev["option"].update(quote)
            ev["option"]["fill_est"] = est_fill(quote, slippage_frac, side)
            ev["option"]["fill_side"] = side
    if note:
        ev["note"] = note
    return ev


def format_alert(item: Item, ev: dict) -> str:
    arrow = "↑" if item.ttype in ("above", "orb_high") else "↓"
    head = {"entry": "TRIGGER", "stop": "STOP HIT", "target": "TARGET HIT",
            "option_stop": "OPTION STOP HIT", "option_target": "OPTION TARGET HIT",
            "cancelled_pair": "CANCELLED"}.get(ev["kind"], ev["kind"].upper())
    t = to_et(ev["time"])
    lines = [f"{head} · {ev['symbol']} {arrow} {ev['trigger_level']:.2f} · last {ev['price']:.2f} · {t:%H:%M:%S} ET"]
    if item.raw.get("play_id"):
        lines.append(f"{item.raw.get('module','')} {item.raw.get('setup','')} {str(item.raw.get('direction','')).upper()} · {item.raw['play_id']}")
    if ev["kind"] == "entry":
        lines.append("CONFIRMED ✅" if ev.get("confirmed") else "NOT CONFIRMED ⚠️ (desk passes for now)")
        for c in ev.get("checks", []):
            lines.append(f"  {'✓' if c['ok'] else '✗'} {c['note']}")
    o = ev.get("option")
    if o:
        if "bid" in o:
            lines.append(f"{o['label']}: bid {o['bid']:.2f} / ask {o['ask']:.2f} (mid {o['mid']:.2f}, spread {o['spread_pct']*100:.1f}%)"
                         + (f" · Δ {abs(o['delta']):.2f}" if o.get("delta") is not None else ""))
            lines.append(f"Paper {'buy' if o['fill_side']=='buy' else 'sell'} est. {o['fill_est']:.2f}")
        else:
            lines.append(f"{o['label']}: no live quote")
    if item.raw.get("plan"):
        lines.append(f"Plan: {item.raw['plan']}")
    if ev.get("source") == "sweep" and ev.get("crossed_at") != ev.get("time"):
        lines.append(f"Level first crossed at {to_et(ev['crossed_at']):%H:%M} ET (caught by the backup sweep).")
    if ev.get("note"):
        lines.append(ev["note"])
    lines.append("Paper mode: nothing was ordered.")
    return "\n".join(lines)

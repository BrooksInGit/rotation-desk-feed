"""Offline tests: python3 -m pytest -q  (or python3 test_core.py)."""
from datetime import date, datetime, timedelta

import desk_core as c
from desk_core import ET, Item

D = date(2026, 11, 9)


def bar(hh, mm, o, h, l, cl, v=1000, vw=None):
    t = (datetime(2026, 11, 9, hh, 0, tzinfo=ET) + timedelta(minutes=mm)).astimezone(c.ZoneInfo("UTC"))
    return {"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": h, "l": l, "c": cl, "v": v, "vw": vw or (h + l) / 2}


def test_time_parsing_nanoseconds():
    t = c.to_et("2026-11-09T15:00:01.123456789Z")
    assert (t.hour, t.minute, t.second) == (10, 0, 1)


def test_trade_filters():
    assert c.trade_crosses("above", 100, 100.01, 200, ["@"])
    assert not c.trade_crosses("above", 100, 100.01, 50, ["@"])      # odd lot
    assert not c.trade_crosses("above", 100, 100.5, 500, ["@", "Z"])  # out of sequence
    assert c.trade_crosses("below", 100, 99.9, 100, None)
    assert not c.trade_crosses("below", 100, 100.1, 100, None)


def test_first_bar_cross_respects_rth_and_arming():
    bars = [bar(9, 25, 99, 101, 98, 100), bar(9, 31, 99, 99.5, 98, 99), bar(10, 2, 99, 100.2, 99, 100)]
    hit = c.first_bar_cross("above", 100, bars, after=None)
    assert c.to_et(hit["t"]).minute == 2  # the 9:25 pre-market bar is ignored
    assert c.first_bar_cross("above", 100, bars, after=datetime(2026, 11, 9, 10, 5, tzinfo=ET)) is None


def test_opening_range():
    bars = [bar(9, 30 + i, 100, 100 + i * 0.1, 99 - i * 0.05, 100) for i in range(30)]
    assert c.opening_range(bars[:29], D, 30) is None  # 9:59 bar not complete yet
    bars.append(bar(10, 0, 100, 105, 90, 100))        # after the window: must not count
    hi, lo = c.opening_range(bars, D, 30)
    assert abs(hi - 102.9) < 1e-9 and abs(lo - (99 - 29 * 0.05)) < 1e-9


def test_session_stats_and_confirmation():
    bars = [bar(9, 30 + i, 100, 100.5, 99.5, 100, v=10000, vw=100) for i in range(60)]
    stats = c.session_stats(bars, D)
    assert abs(stats.vwap - 100) < 1e-9 and stats.volume == 600000
    item = Item({"symbol": "XLE", "kind": "entry", "trigger": {"type": "above", "price": 100.2},
                 "confirmation": {"rvol_min": 1.5, "vwap": "above", "max_extension_pct": 0.01, "spy_block_pct": 0.0075}})
    at = datetime(2026, 11, 9, 10, 30, tzinfo=ET)
    # 600k shares in 1/6.5 of the day vs 2M avg -> 1.95x
    checks = c.confirm_entry(item, "above", 100.2, 100.4, at, stats, 2_000_000, spy_change=-0.002)
    assert all(x.ok for x in checks), checks
    checks = c.confirm_entry(item, "above", 100.2, 102.0, at, stats, 2_000_000, spy_change=-0.01)
    names = {x.name for x in checks if not x.ok}
    assert names == {"extension", "market"}
    early = datetime(2026, 11, 9, 9, 40, tzinfo=ET)
    assert any(x.name == "open" and not x.ok for x in c.confirm_entry(item, "above", 100.2, 100.3, early, stats, 2e6, 0))


def test_occ_and_contract_pick():
    assert c.occ_label("XLE261219C00096000") == "XLE 12/19 $96C"
    p = c.parse_occ("SPY261110P00575500")
    assert p["strike"] == 575.5 and p["type"] == "put"
    chain = {
        "SPY261110C00580000": {"latestQuote": {"bp": 1.00, "ap": 1.04}, "greeks": {"delta": 0.52}},
        "SPY261110C00585000": {"latestQuote": {"bp": 0.40, "ap": 0.60}, "greeks": {"delta": 0.30}},  # delta too low
        "SPY261110C00578000": {"latestQuote": {"bp": 1.80, "ap": 2.20}, "greeks": {"delta": 0.60}},  # spread 20%
        "SPY261120C00580000": {"latestQuote": {"bp": 3.0, "ap": 3.05}, "greeks": {"delta": 0.51}},   # dte 11
    }
    rule = {"dte": [0, 7], "delta": [0.4, 0.6], "max_spread_pct": 0.05, "max_premium": 200}
    sym, q = c.pick_contract(chain, "call", rule, D)
    assert sym == "SPY261110C00580000" and q["mid"] == 1.02
    assert c.est_fill(q, 0.25, "buy") == 1.03 and c.est_fill(q, 0.25, "sell") == 1.01


def test_orb_resolution_and_event_message():
    item = Item({"id": "s1", "play_id": "20261109-scalp-SPY-C", "symbol": "SPY", "kind": "entry", "module": "scalp",
                 "setup": "orb", "direction": "call", "trigger": {"type": "orb_high", "minutes": 30},
                 "plan": "stop -28% / target +40%"})
    assert c.resolve_level(item, None) is None
    assert c.resolve_level(item, (581.2, 578.0)) == ("above", 581.2)
    at = datetime(2026, 11, 9, 10, 12, 3, tzinfo=ET)
    q = {"bid": 1.0, "ask": 1.04, "mid": 1.02, "spread_pct": 0.039, "delta": 0.52, "iv": 0.18, "quote_time": None}
    ev = c.build_event(item, kind="entry", at=at, level=581.2, price=581.3, source="stream",
                       checks=[c.Check("vwap", True, "above VWAP 580.10")], option_symbol="SPY261110C00580000", quote=q)
    assert ev["confirmed"] and ev["option"]["fill_est"] == 1.03 and ev["play_id"] == "20261109-scalp-SPY-C"
    msg = c.format_alert(item, ev)
    assert "TRIGGER · SPY ↑ 581.20" in msg and "SPY 11/10 $580C" in msg and "Paper buy est. 1.03" in msg


def test_same_time_rvol():
    bars = []
    for day in range(2, 9):  # Nov 2-8 (weekend days are skipped by in_rth)
        for i in range(390):
            tt = (datetime(2026, 11, day, 9, 30, tzinfo=ET) + timedelta(minutes=i)).astimezone(c.ZoneInfo("UTC"))
            bars.append({"t": tt.strftime("%Y-%m-%dT%H:%M:%SZ"), "h": 1, "l": 1, "c": 1, "v": 100})
    prof = c.same_time_profile(bars, D, 10)
    assert len(prof) == 5  # Mon-Fri only
    at = datetime(2026, 11, 9, 10, 0, tzinfo=ET)  # minute 30 -> 31 bars = 3100 shares on average
    assert abs(c.rvol_same_time(6200, at, prof) - 2.0) < 1e-9


def test_rs_exemption():
    stats = c.SessionStats(vwap=50.0, volume=1, high=None, low=None)
    at = datetime(2026, 11, 9, 11, 0, tzinfo=ET)
    base = {"symbol": "AMAT", "kind": "entry", "direction": "put", "trigger": {"type": "below", "price": 50},
            "confirmation": {"spy_block_pct": 0.0075}}
    plain = Item(base)
    ex = Item(dict(base, confirmation={"spy_block_pct": 0.0075, "rs_exempt": {"min_move_pct": 0.01, "hard_block_pct": 0.015}}))
    mk = lambda it, spy, und: [x for x in c.confirm_entry(it, "below", 50, 49.9, at, stats, None, spy, underlying_change=und) if x.name == "market"][0]
    assert not mk(plain, 0.009, -0.02).ok           # SPY +0.9% blocks puts
    assert mk(ex, 0.009, -0.02).ok                  # exempt: stock -2% with the trade
    assert not mk(ex, 0.009, -0.005).ok             # stock not moving enough
    assert not mk(ex, 0.016, -0.03).ok              # past the hard block


def test_active_items_filters():
    feed = {"watch": [
        {"symbol": "A", "trigger": {"type": "above", "price": 1}, "expires": "2026-11-08"},
        {"symbol": "B", "trigger": {"type": "above", "price": 1}, "status": "done"},
        {"symbol": "C", "trigger": {"type": "above", "price": 1}, "expires": "2026-11-09"},
        {"symbol": "D"},
    ]}
    assert [i.symbol for i in c.active_items(feed, D)] == ["C"]




def test_reclaim_zone_and_level():
    # AMD 11/11 shape: open 543, flush into the 525 shelf by 9:45-9:50, reclaim VWAP after
    path = [543, 532, 528, 524, 523.2, 526, 530, 534, 539, 543]
    bars = [bar(9, 30 + 5 * i, p + 1, p + 1.5, p - 1.2, p, v=500000) for i, p in enumerate(path)]
    zone = [522.4, 527.6]
    t = c.zone_touch(bars, zone, ["09:45", "11:30"], D)
    assert (t.hour, t.minute) == (9, 45)  # the 9:35/9:40 bars are outside the window
    assert c.zone_touch(bars, zone, ["10:30", "11:30"], D) is None
    vwap = c.session_stats(bars, D).vwap
    assert c.reclaim_level("above", zone, vwap) == max(vwap, 527.6)
    assert c.reclaim_level("below", [560, 565], 533.0) == 533.0
    assert c.in_window(datetime(2026, 11, 9, 11, 29, tzinfo=ET), ["09:45", "11:30"])
    assert not c.in_window(datetime(2026, 11, 9, 11, 30, tzinfo=ET), ["09:45", "11:30"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

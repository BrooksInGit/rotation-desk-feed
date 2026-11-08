#!/usr/bin/env python3
"""Rotation Desk live monitor.

Streams every trade (Alpaca SIP feed) for the symbols the desk is watching,
fires the moment a trigger level breaks, checks the desk's confirmation rules,
captures the live option quote (OPRA) at that moment, sends a Telegram alert
and writes the event back to the feed repo so the desk can use it as the paper
fill. A 30-second sweep of 1-minute bars backs up the stream, so a dropped
connection never means a missed level.

It never trades. It reads prices, sends messages, and commits event files.

Settings come from environment variables; see .env.example.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import websockets

import desk_core as core
from desk_core import ET, Item

log = logging.getLogger("desk-monitor")
DATA = "https://data.alpaca.markets"
CLOCK = "https://paper-api.alpaca.markets/v2/clock"


# ================================================================ settings
def env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing setting {name}. Add it to monitor/.env (see .env.example).")
    return v or ""


class Settings:
    def __init__(self) -> None:
        self.key = env("ALPACA_KEY_ID", required=True)
        self.secret = env("ALPACA_SECRET_KEY", required=True)
        self.feed = env("ALPACA_FEED", "sip")
        self.options_feed = env("ALPACA_OPTIONS_FEED", "opra")
        self.tg_token = env("TELEGRAM_BOT_TOKEN", required=True)
        self.tg_chat = env("TELEGRAM_CHAT_ID", required=True)
        self.repo = Path(env("REPO_DIR", str(Path(__file__).resolve().parent.parent)))
        self.git_push = env("GIT_PUSH", "1") == "1"
        self.sweep_seconds = int(env("SWEEP_SECONDS", "30"))
        self.option_seconds = int(env("OPTION_SECONDS", "15"))
        self.feed_seconds = int(env("FEED_SECONDS", "30"))

    @property
    def watchlist(self) -> Path:
        return self.repo / "watchlist.json"

    def events_file(self, d: date) -> Path:
        return self.repo / "events" / f"{d.isoformat()}.jsonl"


# ================================================================ alpaca REST
class Rest:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.http = requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": s.key, "APCA-API-SECRET-KEY": s.secret})
        self._avg: dict[str, tuple[date, float | None]] = {}

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            r = self.http.get(url, params=params, timeout=15)
            if r.status_code == 429:
                _time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def clock(self) -> dict:
        return self._get(CLOCK)

    def minute_bars(self, symbols: list[str], start: datetime) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        params = {"symbols": ",".join(sorted(set(symbols))), "timeframe": "1Min",
                  "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "feed": self.s.feed, "limit": 10000, "adjustment": "raw"}
        while True:
            j = self._get(f"{DATA}/v2/stocks/bars", params)
            for sym, bars in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            tok = j.get("next_page_token")
            if not tok:
                return out
            params["page_token"] = tok

    def avg_daily_volume(self, sym: str, today: date) -> float | None:
        c = self._avg.get(sym)
        if c and c[0] == today:
            return c[1]
        start = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT00:00:00Z")
        try:
            j = self._get(f"{DATA}/v2/stocks/{sym}/bars", {"timeframe": "1Day", "start": start, "limit": 60,
                                                            "feed": self.s.feed, "adjustment": "split"})
            vols = [b["v"] for b in (j.get("bars") or []) if b["t"][:10] < today.isoformat()][-20:]
            avg = sum(vols) / len(vols) if len(vols) >= 10 else None
        except requests.RequestException as e:
            log.warning("daily bars failed for %s: %s", sym, e)
            avg = None
        self._avg[sym] = (today, avg)
        return avg

    def rvol_profile(self, sym: str, today: date) -> dict:
        c = getattr(self, "_prof", {}).get(sym)
        if c and c[0] == today:
            return c[1]
        start = datetime.combine(today - timedelta(days=21), datetime.min.time(), ET)
        try:
            bars = self.minute_bars([sym], start).get(sym, [])
            prof = core.same_time_profile(bars, today, 10)
        except requests.RequestException as e:
            log.warning("volume profile failed for %s: %s", sym, e)
            prof = {}
        self._prof = getattr(self, "_prof", {})
        self._prof[sym] = (today, prof)
        return prof

    def prev_close(self, sym: str, today: date) -> float | None:
        c = getattr(self, "_prev", {}).get(sym)
        if c and c[0] == today:
            return c[1]
        try:
            snap = self.stock_snapshots([sym]).get(sym) or {}
            pc = (snap.get("prevDailyBar") or {}).get("c")
        except requests.RequestException:
            pc = None
        self._prev = getattr(self, "_prev", {})
        if pc:
            self._prev[sym] = (today, pc)
        return pc

    def stock_snapshots(self, symbols: list[str]) -> dict:
        return self._get(f"{DATA}/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": self.s.feed})

    def option_snapshots(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        j = self._get(f"{DATA}/v1beta1/options/snapshots",
                      {"symbols": ",".join(symbols), "feed": self.s.options_feed})
        return j.get("snapshots") or {}

    def option_chain(self, underlying: str, opt_type: str, exp_from: date, exp_to: date) -> dict:
        out: dict = {}
        params = {"feed": self.s.options_feed, "type": opt_type, "limit": 1000,
                  "expiration_date_gte": exp_from.isoformat(), "expiration_date_lte": exp_to.isoformat()}
        while True:
            j = self._get(f"{DATA}/v1beta1/options/snapshots/{underlying}", params)
            out.update(j.get("snapshots") or {})
            tok = j.get("next_page_token")
            if not tok:
                return out
            params["page_token"] = tok


# ================================================================ repo sync
class Repo:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.pending: list[dict] = []
        self.status: dict = {}

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.s.repo), *args], check=check,
                              capture_output=True, text=True, timeout=60)

    def pull(self) -> None:
        try:
            self._git("pull", "--rebase", "--autostash", "--quiet")
        except Exception as e:
            log.warning("git pull failed, keeping the last watchlist: %s", getattr(e, "stderr", e))

    def load_watchlist(self) -> dict:
        try:
            return json.loads(self.s.watchlist.read_text())
        except FileNotFoundError:
            return {"watch": []}
        except json.JSONDecodeError as e:
            log.error("watchlist.json is not valid JSON (%s); keeping previous items.", e)
            return {}

    def todays_events(self, d: date) -> list[dict]:
        p = self.s.events_file(d)
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def flush(self, now: datetime) -> None:
        """Append pending events, refresh status.json, commit and push."""
        changed = False
        if self.pending:
            by_day: dict[date, list[dict]] = {}
            for ev in self.pending:
                by_day.setdefault(core.to_et(ev["time"]).date(), []).append(ev)
            for d, evs in by_day.items():
                p = self.s.events_file(d)
                p.parent.mkdir(exist_ok=True)
                with p.open("a") as f:
                    for ev in evs:
                        f.write(json.dumps(ev, sort_keys=True) + "\n")
            self.pending.clear()
            changed = True
        st_path = self.s.repo / "status.json"
        new_status = dict(self.status, updated_at=now.astimezone(timezone.utc).isoformat())
        # write status at most every 5 minutes unless events changed
        try:
            old = json.loads(st_path.read_text())
            old_t = datetime.fromisoformat(old.get("updated_at", "1970-01-01T00:00:00+00:00"))
        except Exception:
            old, old_t = {}, datetime.fromtimestamp(0, timezone.utc)
        if changed or now.astimezone(timezone.utc) - old_t > timedelta(minutes=5) or \
                {k: v for k, v in old.items() if k != "updated_at"} != {k: v for k, v in new_status.items() if k != "updated_at"}:
            st_path.write_text(json.dumps(new_status, indent=1, sort_keys=True) + "\n")
            changed = True
        if not changed or not self.s.git_push:
            return
        try:
            self._git("add", "events", "status.json")
            if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return
            self._git("-c", "user.name=desk-monitor", "-c", "user.email=desk-monitor@localhost",
                      "commit", "--quiet", "-m", f"monitor: events/status {now:%Y-%m-%d %H:%M} ET")
            for attempt in range(3):
                if self._git("push", "--quiet", check=False).returncode == 0:
                    return
                self._git("pull", "--rebase", "--quiet", check=False)
            log.error("git push failed three times; will retry next cycle.")
        except Exception as e:
            log.error("git commit/push failed: %s", getattr(e, "stderr", e))


# ================================================================ monitor
class Monitor:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.rest = Rest(s)
        self.repo = Repo(s)
        self.items: list[Item] = []
        self.feed_meta: dict = {}
        self.today: date = datetime.now(ET).date()
        self.fired: set[str] = set()                  # item ids done for today
        self.unconfirmed: dict[str, tuple[float, int]] = {}  # item id -> (last eval epoch, tries)
        self.bars: dict[str, list[dict]] = {}
        self.orb: dict[tuple[str, int], tuple[float, float]] = {}
        self.spy_change: float | None = None
        self.locks: dict[str, asyncio.Lock] = {}
        self.market_open = False
        self.stream_ok = False
        self.stream_down_since: float | None = None
        self.stream_alerted = False
        self.ws = None
        self.subscribed: set[str] = set()
        self.track_only: set[str] = set()

    # ------------------------------------------------------------ helpers
    def stock_items(self) -> list[Item]:
        return [i for i in self.items if i.kind in core.ENTRY_KINDS | core.STOCK_EXIT_KINDS and i.id not in self.fired]

    def option_items(self) -> list[Item]:
        return [i for i in self.items if i.kind in core.OPTION_EXIT_KINDS and i.id not in self.fired and i.option_symbol]

    def symbols(self) -> set[str]:
        return {i.symbol for i in self.stock_items()} | ({"SPY"} if self.stock_items() else set())

    def level_for(self, item: Item) -> tuple[str, float] | None:
        orb = None
        if item.ttype in ("orb_high", "orb_low"):
            orb = self.orb.get((item.symbol, int(item.trigger.get("minutes", 30))))
        return core.resolve_level(item, orb)

    async def tg(self, text: str) -> None:
        def send():
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.s.tg_token}/sendMessage",
                                  json={"chat_id": self.s.tg_chat, "text": text, "disable_web_page_preview": True},
                                  timeout=15)
                if r.status_code != 200:
                    log.error("Telegram %s: %s", r.status_code, r.text[:200])
            except requests.RequestException as e:
                log.error("Telegram failed: %s", e)
        await asyncio.to_thread(send)

    def new_day(self, d: date) -> None:
        self.today = d
        self.fired.clear()
        self.unconfirmed.clear()
        self.track_only.clear()
        self.bars.clear()
        self.orb.clear()
        self.spy_change = None
        # never re-alert something already recorded today (e.g. after a restart)
        for ev in self.repo.todays_events(d):
            if ev.get("track_only"):
                self.track_only.add(ev["item_id"])
            if ev.get("kind") != "entry" or ev.get("confirmed"):
                self.fired.add(ev["item_id"])
            else:
                self.unconfirmed[ev["item_id"]] = (_time.time(), 1)

    # ------------------------------------------------------------ firing
    async def fire(self, item: Item, direction: str, level: float, price: float, at: datetime, source: str,
                   crossed_at: datetime | None = None) -> None:
        lock = self.locks.setdefault(item.id, asyncio.Lock())
        async with lock:
            if item.id in self.fired:
                return
            slip = float(self.feed_meta.get("slippage_frac", 0.25))
            if item.kind in core.ENTRY_KINDS:
                last, tries = self.unconfirmed.get(item.id, (0.0, 0))
                if tries and _time.time() - last < 300:
                    return  # re-check an unconfirmed trigger at most every 5 minutes
                if tries >= 8:
                    return
                await self.refresh_bars([item.symbol, "SPY"])
                stats = core.session_stats(self.bars.get(item.symbol, []), at.date(), upto=at)
                avg = await asyncio.to_thread(self.rest.avg_daily_volume, item.symbol, at.date())
                prof = await asyncio.to_thread(self.rest.rvol_profile, item.symbol, at.date())
                pc = await asyncio.to_thread(self.rest.prev_close, item.symbol, at.date())
                und = (price / pc - 1) if pc else None
                checks = core.confirm_entry(item, direction, level, price, at, stats, avg, self.spy_change,
                                            rvol_profile=prof, underlying_change=und)
                confirmed = all(c.ok for c in checks)
                opt_sym, quote, note = await self.option_for(item, direction, at.date())
                ev = core.build_event(item, kind="entry", at=at, level=level, price=price, source=source,
                                      checks=checks, stats=stats, option_symbol=opt_sym, quote=quote,
                                      slippage_frac=slip, note=note, crossed_at=crossed_at)
                if confirmed:
                    self.fired.add(item.id)
                    self.unconfirmed.pop(item.id, None)
                    grp = item.raw.get("pair")
                    if grp and not item.raw.get("track_only"):
                        # pm_break / orb twins: the first to confirm is the desk's trade; the other keeps being
                        # watched as a tracking-only play so both setups get measured (manual section 12)
                        for other in self.items:
                            if other.id != item.id and other.raw.get("pair") == grp and other.id not in self.fired:
                                other.raw["track_only"] = True
                                self.track_only.add(other.id)
                else:
                    self.unconfirmed[item.id] = (_time.time(), tries + 1)
                    if tries:  # only the first unconfirmed cross gets a message
                        self.repo.pending.append(ev)
                        return
            else:
                self.fired.add(item.id)
                opt_sym, quote, note = await self.option_for(item, item.raw.get("direction", ""), at.date())
                ev = core.build_event(item, kind=item.kind, at=at, level=level, price=price, source=source,
                                      option_symbol=opt_sym, quote=quote, slippage_frac=slip, note=note,
                                      crossed_at=crossed_at)
            self.repo.pending.append(ev)
            log.info("%s %s %s @ %.2f (%s)", ev["kind"], item.symbol, "confirmed" if ev.get("confirmed", True) else "unconfirmed", price, source)
            await self.tg(core.format_alert(item, ev))

    async def option_for(self, item: Item, direction: str, today: date) -> tuple[str | None, dict | None, str]:
        """Live quote for the play's contract, or pick one by rule (scalps)."""
        sym = item.option_symbol
        try:
            if not sym and item.raw.get("contract_rule") and item.kind in core.ENTRY_KINDS:
                rule = item.raw["contract_rule"]
                tmin, tmax = rule.get("dte", [0, 7])
                opt_type = "call" if direction in ("call", "above") else "put"
                chain = await asyncio.to_thread(self.rest.option_chain, item.symbol, opt_type,
                                                today + timedelta(days=tmin), today + timedelta(days=tmax))
                pick = core.pick_contract(chain, opt_type, rule, today)
                if not pick:
                    return None, None, "No contract met the rule (delta / spread / premium)."
                return pick[0], pick[1], "Contract picked at trigger by rule."
            if not sym:
                return None, None, ""
            snaps = await asyncio.to_thread(self.rest.option_snapshots, [sym])
            q = core.quote_from_snapshot(snaps.get(sym))
            return sym, q, "" if q else "Option had no valid bid/ask at trigger."
        except requests.RequestException as e:
            return sym, None, f"Option quote failed: {e}"

    # ------------------------------------------------------------ data
    async def refresh_bars(self, symbols: list[str]) -> None:
        syms = sorted(set(symbols))
        if not syms:
            return
        start = core.session_open(self.today)
        try:
            data = await asyncio.to_thread(self.rest.minute_bars, syms, start)
        except requests.RequestException as e:
            log.warning("minute bars failed: %s", e)
            return
        for sym in syms:
            self.bars[sym] = data.get(sym, [])
        spy = self.bars.get("SPY")
        if spy:
            prev = await asyncio.to_thread(self.rest.prev_close, "SPY", self.today)
            if prev:
                self.spy_change = spy[-1]["c"] / prev - 1

    # ------------------------------------------------------------ loops
    async def clock_loop(self) -> None:
        while True:
            try:
                c = await asyncio.to_thread(self.rest.clock)
                was = self.market_open
                self.market_open = bool(c.get("is_open"))
                if self.market_open != was:
                    log.info("market %s", "open" if self.market_open else "closed")
            except Exception as e:
                now = datetime.now(ET)
                self.market_open = core.in_rth(now)
                log.warning("clock check failed (%s); using 9:30-4:00 ET weekdays", e)
            d = datetime.now(ET).date()
            if d != self.today:
                self.new_day(d)
            await asyncio.sleep(60)

    async def feed_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.repo.pull)
                feed = self.repo.load_watchlist()
                if feed:
                    self.feed_meta = {k: v for k, v in feed.items() if k != "watch"}
                    self.items = core.active_items(feed, self.today)
                    for it in self.items:
                        if it.id in self.track_only:
                            it.raw["track_only"] = True
                await self.sync_subscriptions()
                self.repo.status = {
                    "stream_connected": self.stream_ok, "market_open": self.market_open,
                    "watching": len(self.stock_items()) + len(self.option_items()),
                    "feed": self.s.feed, "options_feed": self.s.options_feed,
                    "watchlist_updated_at": self.feed_meta.get("updated_at"),
                }
                await asyncio.to_thread(self.repo.flush, datetime.now(ET))
                await self.check_stream_health()
            except Exception:
                log.exception("feed loop error")
            await asyncio.sleep(self.s.feed_seconds)

    async def check_stream_health(self) -> None:
        if self.stream_ok:
            self.stream_down_since = None
            if self.stream_alerted:
                self.stream_alerted = False
                await self.tg("Rotation Desk monitor: live stream is back. The 30-second backup sweep covered the gap.")
            return
        if not self.market_open or not self.symbols():
            self.stream_down_since = None
            return
        if self.stream_down_since is None:
            self.stream_down_since = _time.time()
        elif not self.stream_alerted and _time.time() - self.stream_down_since > 300:
            self.stream_alerted = True
            await self.tg("Rotation Desk monitor: live stream has been down 5+ minutes. Still checking every "
                          f"{self.s.sweep_seconds}s from minute bars, so levels are not being missed, just reported later.")

    async def sweep_loop(self) -> None:
        """Backup detection from 1-minute bars; also builds opening ranges and SPY change."""
        while True:
            try:
                if self.market_open and self.stock_items():
                    syms = sorted(self.symbols())
                    await self.refresh_bars(syms)
                    for it in self.stock_items():
                        if it.ttype in ("orb_high", "orb_low"):
                            m = int(it.trigger.get("minutes", 30))
                            r = core.opening_range(self.bars.get(it.symbol, []), self.today, m)
                            if r and (it.symbol, m) not in self.orb:
                                self.orb[(it.symbol, m)] = r
                                log.info("opening range %s %dm: %.2f-%.2f", it.symbol, m, *r)
                        lv = self.level_for(it)
                        if not lv:
                            continue
                        direction, level = lv
                        after = it.armed_after
                        if it.ttype in ("orb_high", "orb_low"):
                            after = core.session_open(self.today) + timedelta(minutes=int(it.trigger.get("minutes", 30)))
                        b = core.first_bar_cross(direction, level, self.bars.get(it.symbol, []), after)
                        if b:
                            # price and option quote are taken now; crossed_at records when the bar hit the level
                            last = self.bars[it.symbol][-1]["c"]
                            await self.fire(it, direction, level, last, datetime.now(ET), "sweep",
                                            crossed_at=core.to_et(b["t"]))
            except Exception:
                log.exception("sweep error")
            await asyncio.sleep(self.s.sweep_seconds)

    async def option_loop(self) -> None:
        """Option-premium stops and targets on open paper positions."""
        while True:
            try:
                items = self.option_items()
                if self.market_open and items:
                    snaps = await asyncio.to_thread(self.rest.option_snapshots, sorted({i.option_symbol for i in items}))
                    now = datetime.now(ET)
                    for it in items:
                        q = core.quote_from_snapshot(snaps.get(it.option_symbol))
                        if not q or it.level is None:
                            continue
                        hit = q["mid"] <= it.level if it.kind == "option_stop" else q["mid"] >= it.level
                        if hit:
                            await self.fire(it, it.ttype or "below", it.level, q["mid"], now, "option_quote")
            except Exception:
                log.exception("option loop error")
            await asyncio.sleep(self.s.option_seconds)

    async def sync_subscriptions(self) -> None:
        want = self.symbols()
        if self.ws is None or not self.stream_ok or want == self.subscribed:
            return
        add, drop = sorted(want - self.subscribed), sorted(self.subscribed - want)
        if add:
            await self.ws.send(json.dumps({"action": "subscribe", "trades": add}))
        if drop:
            await self.ws.send(json.dumps({"action": "unsubscribe", "trades": drop}))
        self.subscribed = want
        log.info("stream symbols: %s", ",".join(sorted(want)) or "(none)")

    async def stream_loop(self) -> None:
        url = os.environ.get("ALPACA_STREAM_URL") or f"wss://stream.data.alpaca.markets/v2/{self.s.feed}"
        backoff = 1
        while True:
            if not self.symbols():
                await asyncio.sleep(10)
                continue
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                    await ws.recv()  # connected
                    await ws.send(json.dumps({"action": "auth", "key": self.s.key, "secret": self.s.secret}))
                    auth = json.loads(await ws.recv())
                    if not any(m.get("msg") == "authenticated" for m in auth):
                        raise RuntimeError(f"stream auth failed: {auth}")
                    self.ws, self.stream_ok, self.subscribed, backoff = ws, True, set(), 1
                    log.info("stream connected (%s)", self.s.feed)
                    await self.sync_subscriptions()
                    async for raw in ws:
                        for m in json.loads(raw):
                            t = m.get("T")
                            if t == "t":
                                await self.on_trade(m)
                            elif t == "error":
                                log.error("stream error: %s", m)
                                if m.get("code") in (406, 407):  # connection limit / slow client
                                    raise RuntimeError(m.get("msg"))
            except Exception as e:
                log.warning("stream disconnected: %s", e)
            finally:
                self.ws, self.stream_ok = None, False
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    async def on_trade(self, m: dict) -> None:
        sym = m.get("S")
        try:
            at = core.to_et(m["t"])
        except Exception:
            return
        if not core.in_rth(at):
            return
        price, size, conds = float(m["p"]), int(m.get("s", 0)), m.get("c") or []
        for it in self.stock_items():
            if it.symbol != sym:
                continue
            lv = self.level_for(it)
            if not lv:
                continue
            direction, level = lv
            if it.armed_after and at < it.armed_after:
                continue
            if core.trade_crosses(direction, level, price, size, conds):
                asyncio.create_task(self.fire(it, direction, level, price, at, "stream"))

    async def run(self) -> None:
        self.new_day(datetime.now(ET).date())
        await asyncio.gather(self.clock_loop(), self.feed_loop(), self.stream_loop(),
                             self.sweep_loop(), self.option_loop())


# ================================================================ entry
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = Settings()
    if "--test" in sys.argv:
        m = Monitor(s)
        asyncio.run(m.tg("Rotation Desk monitor is connected. Alerts will arrive here during market hours."))
        snap = m.rest.stock_snapshots(["SPY"])
        spy = snap.get("SPY", {}).get("latestTrade", {})
        print(f"Telegram test sent. Alpaca {s.feed} feed OK: SPY last {spy.get('p')} at {spy.get('t')}.")
        try:
            ch = m.rest.option_chain("SPY", "call", date.today(), date.today() + timedelta(days=3))
            print(f"Options feed ({s.options_feed}) OK: {len(ch)} SPY call snapshots.")
        except Exception as e:
            print(f"Options feed check failed: {e}")
        return
    asyncio.run(Monitor(s).run())


if __name__ == "__main__":
    main()

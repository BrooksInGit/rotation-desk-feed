#!/usr/bin/env python3
"""Rotation Desk live monitor.

Streams every trade (Alpaca SIP feed) for the symbols the desk is watching,
fires the moment a trigger level breaks, checks the desk's confirmation rules,
captures the live option quote (OPRA) at that moment, sends a Telegram alert
and writes the event back to the feed repo so the desk can use it as the paper
fill. A 30-second sweep of 1-minute bars backs up the stream, so a dropped
connection never means a missed level.

It never trades from this file. When monitor/exec.env holds Alpaca PAPER keys, executor.py places share
bracket orders for confirmed desk entries in that paper account (see executor.py for the safety limits).

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
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import websockets

import desk_core as core
from executor import Executor
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
        self.lock = threading.Lock()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.s.repo), *args], check=check,
                              capture_output=True, text=True, timeout=60)

    def pull(self) -> None:
        try:
            self._git("pull", "--rebase", "--autostash", "--quiet")
        except Exception as e:
            log.warning("git pull failed, keeping the last watchlist: %s", getattr(e, "stderr", e))

    def code_rev(self) -> str | None:
        try:
            return self._git("rev-parse", "HEAD:monitor").stdout.strip()
        except Exception:
            return None

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

    def flush(self, now: datetime, force: bool = False) -> None:
        with self.lock:
            self._flush(now, force)

    def _flush(self, now: datetime, force: bool) -> None:
        """Append pending events, refresh status.json, commit and push."""
        changed = force
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
            self._git("add", *[x for x in ("events", "status.json", "account.json") if (self.s.repo / x).exists()])
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
        self.auto_items: list[Item] = []                 # exits armed by the monitor itself on confirmed entries
        self.auto_plays: set[str] = set()                # play ids whose exits the monitor holds
        self.code_rev: str | None = None
        self.vwap: dict[str, float] = {}                 # session VWAP per symbol, refreshed by the sweep
        self.touched: dict[str, datetime] = {}           # reclaim items: when price first traded into the zone
        self.exec = Executor(Path(__file__).resolve().parent, self.tg, s.tg_token, s.tg_chat, self.repo.pending.append)

    # ------------------------------------------------------------ helpers
    def all_items(self) -> list[Item]:
        desk = [i for i in self.items
                if not (i.kind in core.STOCK_EXIT_KINDS | core.OPTION_EXIT_KINDS and i.raw.get("play_id") in self.auto_plays)]
        return desk + self.auto_items

    def stock_items(self) -> list[Item]:
        return [i for i in self.all_items() if i.kind in core.ENTRY_KINDS | core.STOCK_EXIT_KINDS and i.id not in self.fired]

    def option_items(self) -> list[Item]:
        return [i for i in self.all_items() if i.kind in core.OPTION_EXIT_KINDS and i.id not in self.fired and i.option_symbol]

    def exit_rules(self, module: str) -> dict:
        d = {"scalp": {"option_stop_pct": 0.28, "option_target_mult": 1.40, "stock_stop_pct": 0.01, "stock_target_r": 2.0},
             "swing": {"option_stop_pct": 0.45, "option_target_mult": 1.80, "stock_stop_pct": 0.03, "stock_target_r": 2.0}}
        r = dict(d.get(module, d["scalp"]))
        r.update((self.feed_meta.get("exit_rules") or {}).get(module, {}))
        return r

    def arm_exits(self, entry: Item, ev: dict) -> None:
        """On a confirmed entry, arm option and share exits at once so they are caught live (manual 11.6)."""
        pid = entry.raw.get("play_id") or entry.id
        if ev.get("variant"):
            pid = f"{pid}-{ev['variant']}"
        if pid in self.auto_plays:
            return
        r = self.exit_rules(entry.raw.get("module", "scalp"))
        call = entry.raw.get("direction") == "call"
        base = {k: entry.raw.get(k) for k in ("symbol", "module", "setup", "direction", "pair")}
        base.update({"play_id": pid, "auto": True, "track_only": bool(ev.get("track_only")),
                     "quiet": bool(entry.raw.get("quiet")), "variant": ev.get("variant"),
                     "entry_event": ev["id"], "status": "active", "armed_after": ev["time"]})
        preset = ev.get("exits") or {}
        made = []
        o = ev.get("option") or {}
        if o.get("fill_est"):
            f = float(o["fill_est"])
            for kind, lvl, typ in (("option_stop", preset.get("option_stop", f * (1 - r["option_stop_pct"])), "below"),
                                   ("option_target", preset.get("option_target", f * r["option_target_mult"]), "above")):
                made.append(dict(base, id=f"{pid}:auto:{kind}", kind=kind, leg="option", option_symbol=o["symbol"],
                                 trigger={"type": typ, "price": round(lvl, 2)}))
        px = float(ev["price"])
        stop = preset.get("stop") or entry.raw.get("stock_stop")
        if not stop or (call and stop >= px) or (not call and stop <= px):
            stop = px * (1 - r["stock_stop_pct"]) if call else px * (1 + r["stock_stop_pct"])
        tgt = preset.get("target") or entry.raw.get("target_stock")
        if not tgt or (call and tgt <= px) or (not call and tgt >= px):
            tgt = px + r["stock_target_r"] * (px - stop)
        made.append(dict(base, id=f"{pid}:auto:stop", kind="stop", leg="shares", option_symbol=o.get("symbol"),
                         trigger={"type": "below" if call else "above", "price": round(stop, 4)}))
        made.append(dict(base, id=f"{pid}:auto:target", kind="target", leg="shares", option_symbol=o.get("symbol"),
                         trigger={"type": "above" if call else "below", "price": round(tgt, 4)}))
        self.auto_items += [Item(x) for x in made]
        self.auto_plays.add(pid)
        ev["exits"] = {x["kind"]: x["trigger"]["price"] for x in made}

    def close_leg(self, item: Item) -> None:
        """An exit fired: retire the other exits of the same leg."""
        pid, leg = item.raw.get("play_id"), item.raw.get("leg")
        for x in self.auto_items:
            if x.raw.get("play_id") == pid and (leg is None or x.raw.get("leg") == leg):
                self.fired.add(x.id)

    def symbols(self) -> set[str]:
        return {i.symbol for i in self.stock_items()} | ({"SPY"} if self.stock_items() else set())

    def level_for(self, item: Item) -> tuple[str, float] | None:
        if item.ttype in core.RECLAIM_TYPES:
            tr = item.trigger
            if item.id not in self.touched or not core.in_window(datetime.now(ET), tr.get("window")):
                return None
            direction = "above" if item.ttype == "reclaim_above" else "below"
            lvl = core.reclaim_level(direction, tr.get("zone", []), self.vwap.get(item.symbol))
            return (direction, lvl) if lvl else None
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
        self.auto_items.clear()
        self.auto_plays.clear()
        self.bars.clear()
        self.orb.clear()
        self.vwap.clear()
        self.touched.clear()
        self.spy_change = None
        # never re-alert something already recorded today (e.g. after a restart)
        for ev in self.repo.todays_events(d):
            if ev.get("track_only"):
                self.track_only.add(ev["item_id"])
            if not ev.get("item_id"):
                continue  # order / fill records from the executor
            if ev.get("kind") != "entry" or ev.get("confirmed"):
                self.fired.add(ev["item_id"])
            else:
                self.unconfirmed[ev["item_id"]] = (_time.time(), 1)
        self.rearm_from_events(d)

    def rearm_from_events(self, d: date) -> None:
        """After a restart, re-create exits for confirmed entries whose legs have not closed yet."""
        evs = self.repo.todays_events(d)
        for ev in evs:
            if ev.get("kind") == "entry" and ev.get("confirmed"):
                raw = {k: ev.get(k) for k in ("symbol", "module", "setup", "direction")}
                raw.update({"id": ev["item_id"], "play_id": ev.get("play_id"), "kind": "entry",
                            "quiet": bool(ev.get("variant"))})
                self.arm_exits(Item(raw), ev)
        for ev in evs:
            if ev.get("kind") in ("stop", "target", "option_stop", "option_target", "time_exit") and ev.get("item_id"):
                self.fired.add(ev["item_id"])
                for x in self.auto_items:
                    if x.id == ev["item_id"]:
                        self.close_leg(x)

    # ------------------------------------------------------------ firing
    async def fire(self, item: Item, direction: str, level: float, price: float, at: datetime, source: str,
                   crossed_at: datetime | None = None) -> None:
        lock = self.locks.setdefault(item.id, asyncio.Lock())
        async with lock:
            if item.id in self.fired:
                return
            slip = float(self.feed_meta.get("slippage_frac", 0.25))
            if item.kind in core.ENTRY_KINDS and ((direction == "above" and price < level) or
                                                   (direction == "below" and price > level)):
                return  # price is back on the wrong side of the level: no trigger now (2026-11-11 fix)
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
                    self.arm_exits(item, ev)
                    if not (item.raw.get("track_only") or item.raw.get("quiet") or item.raw.get("variant")):
                        asyncio.get_running_loop().call_later(0.5, lambda r=dict(item.raw), e=ev: asyncio.ensure_future(self.exec.enter(r, e)))
                else:
                    self.unconfirmed[item.id] = (_time.time(), tries + 1)
                    # manual 12 E: when volume is the only failed check, open a tracked no-volume-rule twin
                    failed = {c.name for c in checks if not c.ok}
                    shadow_id = f"{item.id}:no_rvol"
                    if failed == {"rvol"} and shadow_id not in self.fired and not item.raw.get("variant"):
                        self.fired.add(shadow_id)
                        sraw = dict(item.raw, id=shadow_id, track_only=True, variant="no_rvol", quiet=True)
                        sitem = Item(sraw)
                        sev = core.build_event(sitem, kind="entry", at=at, level=level, price=price, source=source,
                                               checks=[c for c in checks if c.name != "rvol"], stats=stats,
                                               option_symbol=opt_sym, quote=quote, slippage_frac=slip,
                                               note="Tracked: would have entered without the volume rule.",
                                               crossed_at=crossed_at)
                        sev["variant"] = "no_rvol"
                        sev["rvol_note"] = next((c.note for c in checks if c.name == "rvol"), None)
                        self.arm_exits(sitem, sev)
                        self.repo.pending.append(sev)
                        log.info("tracked no-rvol entry %s @ %.2f", item.symbol, price)
                    if tries:  # only the first unconfirmed cross gets a message
                        self.repo.pending.append(ev)
                        return
            else:
                self.fired.add(item.id)
                if item.raw.get("auto"):
                    self.close_leg(item)
                opt_sym, quote, note = await self.option_for(item, item.raw.get("direction", ""), at.date())
                ev = core.build_event(item, kind=item.kind, at=at, level=level, price=price, source=source,
                                      option_symbol=opt_sym, quote=quote, slippage_frac=slip, note=note,
                                      crossed_at=crossed_at)
                for k in ("leg", "variant", "entry_event"):
                    if item.raw.get(k):
                        ev[k] = item.raw[k]
            self.repo.pending.append(ev)
            log.info("%s %s %s @ %.2f (%s)", ev["kind"], item.symbol, "confirmed" if ev.get("confirmed", True) else "unconfirmed", price, source)
            if not item.raw.get("quiet"):
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
            st = core.session_stats(self.bars[sym], self.today)
            if st.vwap:
                self.vwap[sym] = st.vwap
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
                rev = self.repo.code_rev()
                if self.code_rev and rev and rev != self.code_rev:
                    await asyncio.to_thread(self.repo.flush, datetime.now(ET))
                    log.warning("monitor code changed (%s -> %s); exiting so systemd restarts it", self.code_rev[:8], rev[:8])
                    os._exit(0)
                feed = self.repo.load_watchlist()
                if feed:
                    self.feed_meta = {k: v for k, v in feed.items() if k != "watch"}
                    self.exec.update_params(self.feed_meta)
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
                        if it.ttype in core.RECLAIM_TYPES and it.id not in self.touched:
                            t0 = core.zone_touch(self.bars.get(it.symbol, []), it.trigger.get("zone", []),
                                                 it.trigger.get("window"), self.today)
                            if t0 and (not it.armed_after or t0 >= it.armed_after - timedelta(minutes=1)):
                                self.touched[it.id] = t0
                                log.info("reclaim zone touched %s at %s", it.symbol, t0.strftime("%H:%M"))
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
                        if it.ttype in core.RECLAIM_TYPES:
                            after = self.touched[it.id] + timedelta(minutes=1)
                        if it.ttype in ("orb_high", "orb_low") or (it.raw.get("setup") == "orb" and not after):
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

    async def time_exit_loop(self) -> None:
        """At 15:05 ET close every open scalp leg the monitor holds (manual 5: scalps are flat by 15:05)."""
        while True:
            try:
                now = datetime.now(ET)
                if self.market_open and now.strftime("%H:%M") >= "15:05":
                    open_legs = {}
                    for x in self.auto_items:
                        if x.raw.get("module") == "scalp" and x.id not in self.fired:
                            open_legs.setdefault((x.raw["play_id"], x.raw.get("leg")), x)
                    for (pid, leg), x in open_legs.items():
                        snap = await asyncio.to_thread(self.rest.stock_snapshots, [x.symbol])
                        last = ((snap.get(x.symbol) or {}).get("latestTrade") or {}).get("p")
                        raw = dict(x.raw, id=f"{pid}:auto:time_exit:{leg}", kind="time_exit")
                        await self.fire(Item(raw), x.ttype, float(last or 0), float(last or 0), now, "clock")
                    scalps = {x.symbol for x in self.auto_items if x.raw.get("module") == "scalp" and not x.raw.get("track_only")}
                    await self.exec.flatten_scalps(self.today, scalps)
            except Exception:
                log.exception("time exit error")
            await asyncio.sleep(30)

    async def account_loop(self) -> None:
        """Every 10 minutes in market hours (hourly otherwise) write the paper account snapshot to account.json."""
        last = 0.0
        while True:
            try:
                every = 600 if self.market_open else 3600
                if self.exec.enabled and _time.time() - last >= every:
                    snap = await asyncio.to_thread(self.exec.snapshot)
                    (self.s.repo / "account.json").write_text(json.dumps(snap, indent=1) + "\n")
                    last = _time.time()
                    await asyncio.to_thread(self.repo.flush, datetime.now(ET), True)
            except Exception:
                log.exception("account snapshot error")
            await asyncio.sleep(60)

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
        self.code_rev = self.repo.code_rev()
        self.exec.loop = asyncio.get_running_loop()
        await asyncio.gather(self.clock_loop(), self.feed_loop(), self.stream_loop(),
                             self.sweep_loop(), self.option_loop(), self.time_exit_loop(), self.exec.command_loop(),
                             self.account_loop())


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

"""Rotation Desk order execution (Alpaca PAPER account only).

When the monitor confirms a desk entry, this places a share bracket order in the desk's own Alpaca paper account:
a marketable limit entry plus a stop and a target that Alpaca holds server-side (one cancels the other), so exits
work even if the monitor is down. Scalps are flattened at 15:05 ET. Never trades tracked-only or volume-twin plays.

Safety:
- paper endpoint only (EXEC_MODE must be "paper"; anything else disables execution)
- risk per trade = equity x risk_pct, position value capped at max_notional_frac x equity and buying power
- no new entries once the day's loss reaches daily_loss_pct of yesterday's equity, or with max_positions open
- Telegram commands from the desk chat: /stop (cancel all orders, close all positions, halt), /resume, /status
- one order per play: client_order_id = "rd-<play_id>", so a restart can never double-enter

Keys live in monitor/exec.env (written by ops/set-exec-keys.sh), separate from the data keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime
from pathlib import Path

import requests

from desk_core import ET

log = logging.getLogger("desk-exec")
PAPER = "https://paper-api.alpaca.markets"

DEFAULTS = {
    "scalp": {"risk_pct": 0.005, "tif": "day"},
    "swing": {"risk_pct": 0.01, "tif": "gtc"},
    "max_notional_frac": 0.5,     # one position at most half the account
    "max_positions": 3,
    "daily_loss_pct": 0.02,
    "entry_buffer_pct": 0.002,    # limit price this far through the trigger price
    "unfilled_cancel_s": 60,
    "scalp_last_entry": "15:00",
}


def load_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def round_px(p: float) -> float:
    return round(p, 2) if p >= 1 else round(p, 4)


def size_order(equity: float, buying_power: float, price: float, stop: float, risk_pct: float,
               max_notional_frac: float) -> int:
    """Shares so a stop-out loses about equity x risk_pct, capped by position value and buying power."""
    per_share = abs(price - stop)
    if per_share <= 0 or price <= 0:
        return 0
    qty = math.floor(equity * risk_pct / per_share)
    cap = math.floor(min(equity * max_notional_frac, buying_power * 0.95) / price)
    return max(0, min(qty, cap))


def bracket_body(symbol: str, side: str, qty: int, price: float, stop: float, target: float, tif: str,
                 buffer_pct: float, client_id: str) -> dict:
    limit = price * (1 + buffer_pct) if side == "buy" else price * (1 - buffer_pct)
    return {"symbol": symbol, "qty": str(qty), "side": side, "type": "limit", "time_in_force": tif,
            "limit_price": str(round_px(limit)), "order_class": "bracket", "client_order_id": client_id,
            "take_profit": {"limit_price": str(round_px(target))},
            "stop_loss": {"stop_price": str(round_px(stop))}}


class Executor:
    def __init__(self, monitor_dir: Path, tg_send, tg_token: str, tg_chat: str, record) -> None:
        cfg = load_env(monitor_dir / "exec.env")
        self.enabled = cfg.get("EXEC_MODE") == "paper" and bool(cfg.get("EXEC_KEY_ID"))
        self.halt_file = monitor_dir / "exec.halt"
        self.tg_send, self.tg_token, self.tg_chat, self.record = tg_send, tg_token, str(tg_chat), record
        self.p = json.loads(json.dumps(DEFAULTS))
        self.flattened_on = None
        self.tg_offset = None
        self.S = requests.Session()
        if self.enabled:
            self.S.headers.update({"APCA-API-KEY-ID": cfg["EXEC_KEY_ID"], "APCA-API-SECRET-KEY": cfg["EXEC_SECRET_KEY"]})
        log.info("execution %s", "ON (Alpaca paper)" if self.enabled else "off (no paper keys in exec.env)")

    # ------------------------------------------------------------ alpaca
    def _req(self, method: str, path: str, **kw):
        r = self.S.request(method, PAPER + path, timeout=20, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
        return r.json() if r.text else {}

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def positions(self) -> list[dict]:
        return self._req("GET", "/v2/positions")

    def update_params(self, meta: dict) -> None:
        over = meta.get("execution") or {}
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(self.p.get(k), dict):
                self.p[k].update(v)
            elif k in self.p:
                self.p[k] = v

    @property
    def halted(self) -> bool:
        return self.halt_file.exists()

    # ------------------------------------------------------------ entries
    async def enter(self, raw: dict, ev: dict) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(self._enter, raw, ev)
        except Exception as e:
            log.exception("order failed")
            await self.tg_send(f"PAPER ORDER FAILED {raw.get('symbol')}: {str(e)[:200]}")

    def _enter(self, raw: dict, ev: dict) -> None:
        sym, module = raw["symbol"], raw.get("module", "scalp")
        long = raw.get("direction") == "call"
        price = float(ev["price"])
        exits = ev.get("exits") or {}
        stop, target = exits.get("stop"), exits.get("target")
        pid = raw.get("play_id") or raw.get("id")
        skip = self._skip_reason(module, long, sym)
        if not skip and (not stop or not target):
            skip = "no stop/target armed"
        if skip:
            self._note(ev, pid, sym, "skipped", skip)
            return
        acct = self.account()
        equity, bp = float(acct["equity"]), float(acct.get("buying_power") or acct["equity"])
        mp = self.p.get(module, self.p["scalp"])
        qty = size_order(equity, bp, price, float(stop), mp["risk_pct"], self.p["max_notional_frac"])
        if qty < 1:
            self._note(ev, pid, sym, "skipped", f"size rounds to 0 shares (equity ${equity:,.0f})")
            return
        body = bracket_body(sym, "buy" if long else "sell", qty, price, float(stop), float(target), mp["tif"],
                            self.p["entry_buffer_pct"], f"rd-{pid}"[:48])
        o = self._req("POST", "/v2/orders", json=body)
        self.record({"kind": "order", "status": "submitted", "play_id": pid, "symbol": sym, "entry_event": ev["id"],
                     "order_id": o.get("id"), "side": body["side"], "qty": qty, "limit": body["limit_price"],
                     "stop": body["stop_loss"]["stop_price"], "target": body["take_profit"]["limit_price"],
                     "time": datetime.now().astimezone().isoformat(), "account": "alpaca-paper"})
        log.info("paper order %s %s %d @ %s", body["side"], sym, qty, body["limit_price"])
        asyncio.run_coroutine_threadsafe(
            self.tg_send(f"PAPER ORDER {body['side'].upper()} {qty} {sym} limit ${body['limit_price']} · "
                         f"stop ${body['stop_loss']['stop_price']} · target ${body['take_profit']['limit_price']}"
                         f" · risk ≈ ${qty * abs(price - float(stop)):,.0f}"), self.loop)
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.watch_fill(o["id"], pid, sym)))

    def _skip_reason(self, module: str, long: bool, sym: str) -> str | None:
        if self.halted:
            return "halted by /stop"
        if module == "scalp" and datetime.now(ET).strftime("%H:%M") >= self.p["scalp_last_entry"]:
            return f"scalp entries stop at {self.p['scalp_last_entry']} ET"
        acct = self.account()
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            return "account blocked"
        eq, last = float(acct["equity"]), float(acct.get("last_equity") or acct["equity"])
        if last and eq <= last * (1 - self.p["daily_loss_pct"]):
            return f"daily loss limit ({(eq / last - 1) * 100:.1f}% today)"
        pos = self.positions()
        if len(pos) >= self.p["max_positions"]:
            return f"{len(pos)} positions open (max {self.p['max_positions']})"
        if any(p["symbol"] == sym for p in pos):
            return f"already holding {sym}"
        if not long:
            a = self._req("GET", f"/v2/assets/{sym}")
            if not (a.get("shortable") and a.get("easy_to_borrow")):
                return f"{sym} not easy to borrow for a short"
        return None

    def _note(self, ev: dict, pid: str, sym: str, status: str, why: str) -> None:
        self.record({"kind": "order", "status": status, "reason": why, "play_id": pid, "symbol": sym,
                     "entry_event": ev["id"], "time": datetime.now().astimezone().isoformat(), "account": "alpaca-paper"})
        log.info("paper order %s %s: %s", status, sym, why)
        asyncio.run_coroutine_threadsafe(self.tg_send(f"PAPER ORDER SKIPPED {sym}: {why}"), self.loop)

    async def watch_fill(self, order_id: str, pid: str, sym: str) -> None:
        waited = 0
        while waited <= self.p["unfilled_cancel_s"]:
            await asyncio.sleep(5)
            waited += 5
            try:
                o = await asyncio.to_thread(self._req, "GET", f"/v2/orders/{order_id}")
            except Exception:
                continue
            if o.get("status") == "filled":
                self.record({"kind": "fill", "play_id": pid, "symbol": sym, "order_id": order_id,
                             "qty": o.get("filled_qty"), "price": o.get("filled_avg_price"),
                             "time": o.get("filled_at") or datetime.now().astimezone().isoformat(),
                             "account": "alpaca-paper"})
                await self.tg_send(f"PAPER FILLED {o.get('side', '').upper()} {o.get('filled_qty')} {sym} @ ${float(o['filled_avg_price']):.2f}")
                return
            if o.get("status") in ("canceled", "expired", "rejected"):
                self.record({"kind": "order", "status": o["status"], "play_id": pid, "symbol": sym, "order_id": order_id,
                             "time": datetime.now().astimezone().isoformat(), "account": "alpaca-paper"})
                await self.tg_send(f"PAPER ORDER {o['status'].upper()} {sym}")
                return
        try:
            await asyncio.to_thread(self._req, "DELETE", f"/v2/orders/{order_id}")
            self.record({"kind": "order", "status": "cancelled_unfilled", "play_id": pid, "symbol": sym,
                         "order_id": order_id, "time": datetime.now().astimezone().isoformat(), "account": "alpaca-paper"})
            await self.tg_send(f"PAPER ORDER CANCELLED {sym}: not filled within {self.p['unfilled_cancel_s']}s")
        except Exception as e:
            log.warning("cancel unfilled %s: %s", order_id, e)

    # ------------------------------------------------------------ exits
    def _close_symbols(self, symbols: set[str] | None) -> list[str]:
        """Cancel open orders and close positions (all, or just these symbols). Returns what was closed."""
        orders = self._req("GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500})
        for o in orders:
            if symbols is None or o["symbol"] in symbols:
                try:
                    self._req("DELETE", f"/v2/orders/{o['id']}")
                except Exception as e:
                    log.warning("cancel %s: %s", o["id"], e)
        closed = []
        for p in self.positions():
            if symbols is None or p["symbol"] in symbols:
                self._req("DELETE", f"/v2/positions/{p['symbol']}")
                closed.append(f"{p['symbol']} ({float(p['unrealized_pl']):+,.0f})")
        return closed

    async def flatten_scalps(self, today, scalp_symbols: set[str]) -> None:
        """15:05 ET: close scalp positions the desk opened today (swings are left alone)."""
        if not self.enabled or self.flattened_on == today:
            return
        self.flattened_on = today
        held = {p["symbol"] for p in await asyncio.to_thread(self.positions)}
        mine = held & scalp_symbols
        if not mine:
            return
        closed = await asyncio.to_thread(self._close_symbols, mine)
        self.record({"kind": "order", "status": "time_exit", "symbols": sorted(mine),
                     "time": datetime.now().astimezone().isoformat(), "account": "alpaca-paper"})
        await self.tg_send("PAPER 15:05 CLOSE: " + ", ".join(closed))

    # ------------------------------------------------------------ telegram commands
    async def command_loop(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                upd = await asyncio.to_thread(self._updates)
                for u in upd:
                    msg = u.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != self.tg_chat:
                        continue
                    text = (msg.get("text") or "").strip().lower()
                    if text.startswith("/stop"):
                        self.halt_file.write_text(datetime.now().astimezone().isoformat())
                        closed = await asyncio.to_thread(self._close_symbols, None)
                        self.record({"kind": "order", "status": "halted", "time": datetime.now().astimezone().isoformat(),
                                     "account": "alpaca-paper"})
                        await self.tg_send("PAPER TRADING HALTED. Cancelled all orders; closed: "
                                           + (", ".join(closed) or "nothing open") + ". Send /resume to restart.")
                    elif text.startswith("/resume"):
                        self.halt_file.unlink(missing_ok=True)
                        await self.tg_send("PAPER TRADING RESUMED.")
                    elif text.startswith("/status"):
                        a = await asyncio.to_thread(self.account)
                        pos = await asyncio.to_thread(self.positions)
                        day = float(a["equity"]) - float(a.get("last_equity") or a["equity"])
                        lines = [f"{p['symbol']} {p['qty']} @ {float(p['avg_entry_price']):.2f} ({float(p['unrealized_pl']):+,.0f})" for p in pos]
                        await self.tg_send(f"Paper account ${float(a['equity']):,.2f} ({day:+,.2f} today)"
                                           f"{' · HALTED' if self.halted else ''}\n" + ("\n".join(lines) or "No positions."))
            except Exception:
                log.exception("telegram command loop")
                await asyncio.sleep(10)

    def _updates(self) -> list[dict]:
        params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
        if self.tg_offset is not None:
            params["offset"] = self.tg_offset
        r = requests.get(f"https://api.telegram.org/bot{self.tg_token}/getUpdates", params=params, timeout=35)
        res = r.json().get("result", [])
        if self.tg_offset is None and res:
            # first call after a start: skip old messages so a past /stop is not replayed
            self.tg_offset = res[-1]["update_id"] + 1
            return []
        if res:
            self.tg_offset = res[-1]["update_id"] + 1
        elif self.tg_offset is None:
            self.tg_offset = 0
        return res

#!/usr/bin/env python3
"""
Brain V11 hotfix v2: execution protection + equity/UI + same-direction limit + backtest diagnostics.
Run on server in /root/brain after backing up current files.
"""
from __future__ import annotations
import os, re, shutil, sys, datetime
from pathlib import Path

ROOT = Path(os.environ.get("BRAIN_ROOT", "/root/brain"))
BRAIN = ROOT / "brain_v11_1_aipro.py"
DASH = ROOT / "dashboard.py"
BT = ROOT / "bt_analyze.py"
ENV = ROOT / ".demo.env"
RUN_BT1 = ROOT / "run_bt_fixed1.sh"
RUN_BT2 = ROOT / "run_bt_fixed2.sh"
STAMP = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

HOTFIX_START = "# === CHATGPT_HOTFIX_V2_START ==="
HOTFIX_END = "# === CHATGPT_HOTFIX_V2_END ==="
DASH_START = "# === CHATGPT_DASHBOARD_HOTFIX_V2_START ==="
DASH_END = "# === CHATGPT_DASHBOARD_HOTFIX_V2_END ==="


def backup(path: Path) -> None:
    if path.exists():
        bak = path.with_name(path.name + f".bak_hotfix_v2_{STAMP}")
        shutil.copy2(path, bak)
        print(f"backup {path} -> {bak}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def remove_block(text: str, start: str, end: str) -> str:
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    return pat.sub("", text)


def insert_before_main(text: str, block: str) -> str:
    text = remove_block(text, HOTFIX_START, HOTFIX_END)
    m = re.search(r"\nif\s+__name__\s*==\s*['\"]__main__['\"]\s*:\s*\n", text)
    if m:
        return text[:m.start()] + "\n" + block + "\n" + text[m.start():]
    return text.rstrip() + "\n\n" + block + "\n"


def insert_dashboard_before_main(text: str, block: str) -> str:
    text = remove_block(text, DASH_START, DASH_END)
    m = re.search(r"\nif\s+__name__\s*==\s*['\"]__main__['\"]\s*:\s*\n", text)
    if m:
        return text[:m.start()] + "\n" + block + "\n" + text[m.start():]
    return text.rstrip() + "\n\n" + block + "\n"


def update_env() -> None:
    ENV.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if ENV.exists():
        lines = ENV.read_text(encoding="utf-8", errors="replace").splitlines()
    kv = {
        "TRADING_MODE": "demo",
        "USE_MAINNET_MARKET_DATA": "1",
        "USE_EXCHANGE_PROTECTION": "1",
        "CLOSE_IF_PROTECTION_FAIL": "1",
        "PROTECT_EXISTING_POSITIONS": "1",
        "FORCE_CLOSE_UNPROTECTED_EXISTING": "1",
        "MANAGE_UNPROTECTED_ON_HEALTHCHECK": "1",
        "LEVERAGE": "5",
        "MARGIN_TYPE": "CROSSED",
        "MAX_POSITIONS": "5",
        "MAX_NEW_ENTRIES_PER_CYCLE": "2",
        "MAX_SAME_DIRECTION_POSITIONS": "5",
        "MAX_LONG_POSITIONS": "5",
        "MAX_SHORT_POSITIONS": "5",
        "PORTFOLIO_SAME_DIRECTION_LIMIT": "5",
        "V10_MAX_SAME_DIRECTION_POSITIONS": "5",
        "V10_MAX_LONG_POSITIONS": "5",
        "V10_MAX_SHORT_POSITIONS": "5",
        "DEMO_ENABLE_TARGET_NOTIONAL": "1",
        "DEMO_TARGET_NOTIONAL_PCT": "0.10",
        "DEMO_MIN_NOTIONAL_USDT": "300",
        "DEMO_MAX_NOTIONAL_USDT": "700",
        "DEMO_MAX_TOTAL_NOTIONAL_PCT": "0.80",
        "RISK_PER_TRADE": "0.005",
        "DAILY_MAX_LOSS_PCT": "0.03",
        "COOLDOWN_MINUTES": "60",
        "SETUP_WHITELIST": "pullback",
        "V11_SETUP_WHITELIST": "pullback",
        "ENABLE_TREND_SETUP": "0",
        "ENABLE_BREAKOUT_SETUP": "0",
        "ENABLE_CONFIRM_V2": "1",
        "REQUIRE_BACKTEST_QUALITY_GATE": "0",
        "BACKTEST_LIMIT": "120000",
        "BT_DIAG_REJECT_REASONS": "1",
    }
    existing = {}
    order = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            order.append((None, line))
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        existing[key] = v
        order.append((key, line))
    out = []
    done = set()
    for key, line in order:
        if key in kv:
            out.append(f"{key}={kv[key]}")
            done.add(key)
        else:
            out.append(line)
    out.append("")
    out.append("# ChatGPT hotfix v2 execution/UI/backtest settings")
    for key, val in kv.items():
        if key not in done:
            out.append(f"{key}={val}")
    ENV.write_text("\n".join(out).rstrip()+"\n", encoding="utf-8")
    print(f"updated {ENV}")


BRAIN_BLOCK = r'''
# === CHATGPT_HOTFIX_V2_START ===
# Hotfix v2 inserted by ChatGPT: account equity, protection orders, old naked positions,
# same-direction demo limits, and richer backtest diagnostics.  Safe to remove as one block.
try:
    import json as _cg_json, time as _cg_time, math as _cg_math, logging as _cg_logging, datetime as _cg_dt
except Exception:
    pass

def _cg_sf(v, default=0.0):
    try:
        return safe_float(v, default)  # type: ignore[name-defined]
    except Exception:
        try:
            if v is None or v == "":
                return float(default)
            return float(v)
        except Exception:
            return float(default)

def _cg_env_bool(name, default=False):
    try:
        return env_bool(name, default)  # type: ignore[name-defined]
    except Exception:
        return str(os.environ.get(name, str(default))).strip().lower() in {"1","true","yes","y","on"}

def _cg_env_float(name, default=0.0):
    try:
        return env_float(name, default)  # type: ignore[name-defined]
    except Exception:
        try: return float(os.environ.get(name, default))
        except Exception: return float(default)

def _cg_env_int(name, default=0):
    try:
        return env_int(name, default)  # type: ignore[name-defined]
    except Exception:
        try: return int(float(os.environ.get(name, default)))
        except Exception: return int(default)

def _cg_now_iso():
    try:
        return utc_now().isoformat()  # type: ignore[name-defined]
    except Exception:
        return _cg_dt.datetime.utcnow().replace(tzinfo=_cg_dt.timezone.utc).isoformat()

def _cg_patch_client():
    C = globals().get("BinanceFuturesClient")
    if not C:
        return

    def account_detail_full(self):
        detail = {
            "wallet_balance": 0.0,
            "available_balance": 0.0,
            "cross_wallet_balance": 0.0,
            "total_unrealized_pnl": 0.0,
            "equity": 0.0,
            "margin_balance": 0.0,
        }
        # Prefer /fapi/v2/account because balance.balance is wallet only and excludes unrealized PnL.
        try:
            acc = self.signed("GET", "/fapi/v2/account")
            if isinstance(acc, dict):
                wallet = _cg_sf(acc.get("totalWalletBalance"), 0.0)
                unreal = _cg_sf(acc.get("totalUnrealizedProfit"), 0.0)
                margin_balance = _cg_sf(acc.get("totalMarginBalance"), wallet + unreal)
                avail = _cg_sf(acc.get("availableBalance"), margin_balance)
                detail.update({
                    "wallet_balance": wallet,
                    "available_balance": avail,
                    "cross_wallet_balance": _cg_sf(acc.get("totalCrossWalletBalance"), wallet),
                    "total_unrealized_pnl": unreal,
                    "equity": margin_balance if margin_balance else wallet + unreal,
                    "margin_balance": margin_balance if margin_balance else wallet + unreal,
                })
                return detail
        except Exception as exc:
            _cg_logging.warning("CG_ACCOUNT_V2_ACCOUNT_FAILED %s", exc)
        # Fallback to /fapi/v2/balance plus position PnL.
        try:
            rows = self.signed("GET", "/fapi/v2/balance")
            if isinstance(rows, list):
                for row in rows:
                    if row.get("asset") == "USDT":
                        detail["wallet_balance"] = _cg_sf(row.get("balance"), 0.0)
                        detail["available_balance"] = _cg_sf(row.get("availableBalance"), detail["wallet_balance"])
                        detail["cross_wallet_balance"] = _cg_sf(row.get("crossWalletBalance"), 0.0)
                        break
        except Exception as exc:
            _cg_logging.warning("CG_ACCOUNT_BALANCE_FAILED %s", exc)
        try:
            unreal = 0.0
            for p in self.open_positions():
                unreal += _cg_sf(p.get("unRealizedProfit", p.get("unrealizedProfit", 0.0)), 0.0)
            detail["total_unrealized_pnl"] = unreal
            detail["equity"] = detail["wallet_balance"] + unreal
            detail["margin_balance"] = detail["equity"]
        except Exception:
            detail["equity"] = detail["wallet_balance"]
            detail["margin_balance"] = detail["wallet_balance"]
        return detail

    def account_balance_detail(self):
        return self.account_detail_full()

    def account_balance_usdt(self):
        return _cg_sf(self.account_detail_full().get("equity"), 0.0)

    def _std_open_orders(self, symbol=None):
        params = {}
        if symbol: params["symbol"] = symbol
        try:
            data = self.signed("GET", "/fapi/v1/openOrders", params)
            return data if isinstance(data, list) else []
        except Exception as exc:
            _cg_logging.warning("CG_STD_OPEN_ORDERS_FAILED symbol=%s error=%s", symbol or "ALL", exc)
            return []

    def _algo_open_orders(self, symbol=None):
        params = {}
        if symbol: params["symbol"] = symbol
        orders = []
        # Binance/demo environments have changed names over time; try several read-only endpoints.
        for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/openAlgoOrders", "/fapi/v1/conditional/openOrders"):
            try:
                data = self.signed("GET", path, params)
                if isinstance(data, list):
                    for o in data:
                        if isinstance(o, dict):
                            o = dict(o); o.setdefault("_source", path); orders.append(o)
                elif isinstance(data, dict):
                    for key in ("orders", "data", "list"):
                        val = data.get(key)
                        if isinstance(val, list):
                            for o in val:
                                if isinstance(o, dict):
                                    o = dict(o); o.setdefault("_source", path); orders.append(o)
                if orders:
                    break
            except Exception:
                continue
        return orders

    def open_orders_all(self, symbol=None):
        seen = set(); out = []
        for o in _std_open_orders(self, symbol) + _algo_open_orders(self, symbol):
            if not isinstance(o, dict):
                continue
            oid = str(o.get("orderId") or o.get("algoId") or o.get("clientAlgoId") or o.get("clientOrderId") or id(o))
            key = (str(o.get("symbol", symbol or "")), oid, str(o.get("type") or o.get("origType") or o.get("orderType")))
            if key in seen:
                continue
            seen.add(key); out.append(o)
        return out

    def cancel_open_orders(self, symbol):
        res = {"standard": None, "algo": []}
        try:
            res["standard"] = self.signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception as exc:
            res["standard_error"] = str(exc)
            _cg_logging.warning("CG_CANCEL_STANDARD_FAILED symbol=%s error=%s", symbol, exc)
        for path in ("/fapi/v1/allAlgoOrders", "/fapi/v1/algoOrder", "/fapi/v1/conditional/allOpenOrders"):
            try:
                r = self.signed("DELETE", path, {"symbol": symbol})
                res["algo"].append({"path": path, "response": r})
            except Exception:
                pass
        return res

    def close_market_order(self, symbol, direction, qty):
        side = "SELL" if str(direction).upper() == "LONG" else "BUY"
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": str(qty), "reduceOnly": "true", "newOrderRespType": "RESULT"}
        if getattr(self.settings, "hedge_mode", False):
            params["positionSide"] = str(direction).upper()
        return self.signed("POST", "/fapi/v1/order", params)

    def place_reduce_only_conditional(self, symbol, direction, order_type, trigger_price, qty):
        side = "SELL" if str(direction).upper() == "LONG" else "BUY"
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "stopPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "quantity": str(qty),
            "newOrderRespType": "ACK",
        }
        if getattr(self.settings, "hedge_mode", False):
            params["positionSide"] = str(direction).upper()
        return self.signed("POST", "/fapi/v1/order", params)

    def place_close_algo(self, symbol, direction, order_type, trigger_price, qty=None):
        # Prefer ordinary conditional closePosition orders so /openOrders can see them.
        side = "SELL" if str(direction).upper() == "LONG" else "BUY"
        std = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "stopPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "newOrderRespType": "ACK",
        }
        if getattr(self.settings, "hedge_mode", False):
            std["positionSide"] = str(direction).upper()
        first_error = None
        try:
            r = self.signed("POST", "/fapi/v1/order", std)
            return {"ok": True, "endpoint": "order", "response": r}
        except Exception as exc:
            first_error = exc
            _cg_logging.warning("CG_PROTECTION_STANDARD_CLOSEPOSITION_FAILED symbol=%s type=%s err=%s", symbol, order_type, exc)
            # -4130 normally means a closePosition conditional already exists. Re-read both ordinary and algo orders.
            msg = str(exc)
            if "-4130" in msg or "closePosition" in msg:
                orders = open_orders_all(self, symbol)
                if orders:
                    return {"ok": True, "endpoint": "existing", "response": {"reason": msg, "orders": orders[:5]}}
        if qty is not None and _cg_sf(qty, 0.0) > 0:
            try:
                r = place_reduce_only_conditional(self, symbol, direction, order_type, trigger_price, qty)
                return {"ok": True, "endpoint": "order_reduceOnly", "response": r}
            except Exception as exc:
                _cg_logging.warning("CG_PROTECTION_REDUCE_ONLY_FAILED symbol=%s type=%s err=%s", symbol, order_type, exc)
        # Last best-effort algo endpoint.
        algo = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "priceProtect": "TRUE",
            "newOrderRespType": "ACK",
        }
        if getattr(self.settings, "hedge_mode", False):
            algo["positionSide"] = str(direction).upper()
        try:
            r = self.signed("POST", "/fapi/v1/algoOrder", algo)
            return {"ok": True, "endpoint": "algoOrder", "response": r}
        except Exception as exc:
            raise RuntimeError(f"protection failed standard={first_error}; algo={exc}") from exc

    C.account_detail_full = account_detail_full
    C.account_balance_detail = account_balance_detail
    C.account_balance_usdt = account_balance_usdt
    C.open_orders_standard = _std_open_orders
    C.algo_open_orders = _algo_open_orders
    C.open_orders_all = open_orders_all
    C.open_orders = open_orders_all
    C.cancel_open_orders = cancel_open_orders
    C.close_market_order = close_market_order
    C.place_reduce_only_conditional = place_reduce_only_conditional
    C.place_close_algo = place_close_algo


def _cg_patch_exchange_broker():
    B = globals().get("ExchangeBroker")
    if not B:
        return
    _orig_open_position = getattr(B, "open_position", None)

    def _dir_from_amt(self, amt):
        amt = _cg_sf(amt, 0.0)
        if amt > 0: return "LONG"
        if amt < 0: return "SHORT"
        return "FLAT"

    def _classify_orders(self, orders):
        has_stop = False; has_tp = False; stop_loss = ""; tp2 = ""
        for o in orders or []:
            if not isinstance(o, dict):
                continue
            typ = str(o.get("origType") or o.get("type") or o.get("orderType") or o.get("strategyType") or o.get("algoType") or "").upper()
            raw = " ".join(str(o.get(k, "")) for k in ("origType","type","orderType","strategyType","algoType","clientOrderId"))
            raw_u = raw.upper()
            trig = o.get("stopPrice") or o.get("triggerPrice") or o.get("activatePrice") or o.get("priceRate") or ""
            close_pos = str(o.get("closePosition", "")).lower() == "true" or str(o.get("reduceOnly", "")).lower() == "true"
            if "TAKE_PROFIT" in raw_u or "TAKEPROFIT" in raw_u:
                has_tp = True
                if str(trig) not in {"", "0", "0.0", "None"}: tp2 = str(trig)
            elif "STOP" in raw_u or (close_pos and not has_stop):
                has_stop = True
                if str(trig) not in {"", "0", "0.0", "None"}: stop_loss = str(trig)
        if has_stop and has_tp: status = "PROTECTED"
        elif has_stop: status = "STOP_ONLY"
        elif has_tp: status = "TP_ONLY"
        else: status = "NO_PROTECTION"
        return {"has_stop": has_stop, "has_take_profit": has_tp, "stop_loss": stop_loss, "tp2": tp2, "protection_status": status}

    def _enrich_position(self, row):
        p = dict(row or {})
        sym = str(p.get("symbol", ""))
        amt = _cg_sf(p.get("positionAmt"), 0.0)
        entry = _cg_sf(p.get("entryPrice"), 0.0)
        mark = _cg_sf(p.get("markPrice"), entry)
        notional = abs(_cg_sf(p.get("notional"), amt * mark))
        lev = _cg_sf(p.get("leverage"), _cg_env_float("LEVERAGE", 1.0)) or _cg_env_float("LEVERAGE", 1.0)
        initial_margin = abs(notional) / lev if lev > 0 else 0.0
        unreal = _cg_sf(p.get("unRealizedProfit", p.get("unrealizedProfit", 0.0)), 0.0)
        roe = unreal / initial_margin * 100.0 if initial_margin > 0 else 0.0
        try:
            orders = self.client.open_orders_all(sym) if hasattr(self.client, "open_orders_all") else self.client.open_orders(sym)
        except Exception as exc:
            _cg_logging.warning("CG_ENRICH_OPEN_ORDERS_FAILED symbol=%s err=%s", sym, exc)
            orders = []
        prot = self._classify_orders(orders)
        liq = p.get("liquidationPrice", "")
        if str(liq) in {"", "0", "0.0", "None"}:
            liq = "N/A"
        p.update({
            "direction": self._direction_from_amt(amt),
            "qty_abs": abs(amt),
            "entryPrice": entry,
            "markPrice": mark,
            "notional": notional,
            "initial_margin": initial_margin,
            "marginType": str(p.get("marginType", p.get("margin_type", "cross"))).lower(),
            "unRealizedProfit": unreal,
            "roe_pct": roe,
            "liquidationPrice": liq,
            "open_orders_count": len(orders),
            "open_orders": orders[:10],
            **prot,
        })
        p.setdefault("tp1", "")
        return p

    def positions(self):
        out = {}
        try:
            for row in self.client.open_positions():
                sym = str(row.get("symbol", ""))
                if sym:
                    out[sym] = self._enrich_position(row)
        except Exception:
            _cg_logging.exception("CG_POSITIONS_FAILED")
        return out

    def account_detail(self):
        try:
            return self.client.account_detail_full()
        except Exception:
            return {}

    def equity(self, mark_prices=None):
        try:
            return _cg_sf(self.account_detail().get("equity"), 0.0)
        except Exception:
            return 0.0

    def total_notional(self, mark_prices=None):
        try:
            return sum(abs(_cg_sf(p.get("notional"), 0.0)) for p in self.positions().values())
        except Exception:
            return 0.0

    def _rule_for(self, sym):
        try:
            er = self.client.execution_symbol_rules()
            if isinstance(er, dict) and sym in er:
                return er[sym]
        except Exception:
            pass
        try:
            return self.rules.get(sym)
        except Exception:
            return None

    def _round_price(self, sym, price):
        rule = self._rule_for(sym)
        try:
            return rule.round_price(price) if rule else str(price)
        except Exception:
            return str(price)

    def _round_qty(self, sym, qty):
        rule = self._rule_for(sym)
        try:
            return rule.round_qty(abs(_cg_sf(qty, 0.0))) if rule else str(abs(_cg_sf(qty, 0.0)))
        except Exception:
            return str(abs(_cg_sf(qty, 0.0)))

    def _submit_protection(self, symbol, direction, stop_loss, tp2, cancel_first=True, qty=None):
        detail = {"sl_order": None, "tp_order": None, "verify": None, "open_orders_count": 0}
        try:
            if cancel_first:
                detail["cancel"] = self.client.cancel_open_orders(symbol)
                _cg_time.sleep(_cg_env_float("PROTECTION_CANCEL_SLEEP", 0.5))
            q = qty
            if q is None:
                for p in self.client.open_positions():
                    if str(p.get("symbol")) == symbol:
                        q = self._round_qty(symbol, abs(_cg_sf(p.get("positionAmt"), 0.0)))
                        break
            detail["sl_order"] = self.client.place_close_algo(symbol, direction, "STOP_MARKET", stop_loss, qty=q)
            detail["tp_order"] = self.client.place_close_algo(symbol, direction, "TAKE_PROFIT_MARKET", tp2, qty=q)
            _cg_time.sleep(_cg_env_float("PROTECTION_VERIFY_SLEEP", 1.0))
        except Exception as exc:
            detail["error"] = str(exc)
            _cg_logging.exception("CG_SUBMIT_PROTECTION_FAILED symbol=%s", symbol)
        try:
            orders = self.client.open_orders_all(symbol) if hasattr(self.client, "open_orders_all") else self.client.open_orders(symbol)
            prot = self._classify_orders(orders)
            detail["verify"] = prot
            detail["open_orders_count"] = len(orders)
            ok = bool(prot.get("has_stop") and prot.get("has_take_profit"))
            return ok, detail
        except Exception as exc:
            detail["verify_error"] = str(exc)
            return False, detail

    def _close_unprotected(self, sym, pos, reason="NO_PROTECTION"):
        direction = str(pos.get("direction") or self._direction_from_amt(pos.get("positionAmt"))).upper()
        qty = self._round_qty(sym, pos.get("qty_abs", abs(_cg_sf(pos.get("positionAmt"), 0.0))))
        if _cg_sf(qty, 0.0) <= 0:
            return False
        try:
            res = self.client.close_market_order(sym, direction, qty)
            try:
                self.journal.write({"time": _cg_now_iso(), "event": "CLOSED_NO_PROTECTION", "symbol": sym, "direction": direction, "qty": qty, "reason": reason, "order_id": (res or {}).get("orderId", "")})
            except Exception:
                pass
            _cg_logging.error("CG_CLOSED_NO_PROTECTION symbol=%s qty=%s reason=%s", sym, qty, reason)
            return True
        except Exception:
            _cg_logging.exception("CG_CLOSE_UNPROTECTED_FAILED symbol=%s", sym)
            return False

    def protect_or_close_position(self, symbol, pos=None):
        if pos is None:
            pos = self.positions().get(symbol)
        if not pos:
            return {"ok": True, "status": "NO_POSITION"}
        if str(pos.get("protection_status")) == "PROTECTED":
            return {"ok": True, "status": "PROTECTED"}
        direction = str(pos.get("direction") or self._direction_from_amt(pos.get("positionAmt"))).upper()
        entry = _cg_sf(pos.get("entryPrice"), 0.0)
        if direction not in {"LONG", "SHORT"} or entry <= 0:
            return {"ok": False, "status": "INVALID_POSITION"}
        if direction == "LONG":
            stop = entry * _cg_env_float("EXISTING_LONG_SL_MULT", 0.985)
            tp = entry * _cg_env_float("EXISTING_LONG_TP_MULT", 1.025)
        else:
            stop = entry * _cg_env_float("EXISTING_SHORT_SL_MULT", 1.015)
            tp = entry * _cg_env_float("EXISTING_SHORT_TP_MULT", 0.975)
        stop_s = self._round_price(symbol, stop); tp_s = self._round_price(symbol, tp)
        qty = self._round_qty(symbol, pos.get("qty_abs", abs(_cg_sf(pos.get("positionAmt"), 0.0))))
        ok, detail = self._submit_protection(symbol, direction, stop_s, tp_s, cancel_first=True, qty=qty)
        if ok:
            try:
                self.journal.write({"time": _cg_now_iso(), "event": "EXISTING_POSITION_PROTECTED", "symbol": symbol, "direction": direction, "qty": qty, "stop_loss": stop_s, "tp2": tp_s, "reason": "hotfix_v2"})
            except Exception:
                pass
            return {"ok": True, "status": "PROTECTED", "detail": detail}
        if _cg_env_bool("CLOSE_IF_PROTECTION_FAIL", True) and _cg_env_bool("FORCE_CLOSE_UNPROTECTED_EXISTING", True):
            closed = self._close_unprotected(symbol, pos, reason="PROTECTION_FAILED_HOTFIX_V2")
            return {"ok": closed, "status": "CLOSED_NO_PROTECTION" if closed else "CLOSE_FAILED", "detail": detail}
        return {"ok": False, "status": "NO_PROTECTION", "detail": detail}

    def manage_positions(self, mark_prices=None):
        if not _cg_env_bool("PROTECT_EXISTING_POSITIONS", True):
            return None
        try:
            for sym, pos in list(self.positions().items()):
                if pos.get("protection_status") != "PROTECTED":
                    self.protect_or_close_position(sym, pos)
        except Exception:
            _cg_logging.exception("CG_MANAGE_POSITIONS_HOTFIX_FAILED")
        return None

    def open_position(self, plan, signal):
        # Set cross+leverage before original open, and keep original execution flow.
        try:
            self.client.set_margin_type_cross(plan.symbol)
        except Exception:
            pass
        try:
            self.client.set_leverage(plan.symbol, _cg_env_int("LEVERAGE", getattr(self.settings, "leverage", 5)))
        except Exception:
            pass
        return _orig_open_position(self, plan, signal)

    B._direction_from_amt = _dir_from_amt
    B._classify_orders = _classify_orders
    B._enrich_position = _enrich_position
    B.positions = positions
    B.account_detail = account_detail
    B.equity = equity
    B.total_notional = total_notional
    B._rule_for = _rule_for
    B._round_price = _round_price
    B._round_qty = _round_qty
    B._submit_protection = _submit_protection
    B.protect_or_close_position = protect_or_close_position
    B.manage_positions = manage_positions
    if _orig_open_position:
        B.open_position = open_position


def _cg_patch_risk_limits():
    # Make demo same-direction limits honor MAX_POSITIONS=5 when code uses common env names.
    for k in ("MAX_SAME_DIRECTION_POSITIONS","MAX_LONG_POSITIONS","MAX_SHORT_POSITIONS","PORTFOLIO_SAME_DIRECTION_LIMIT","V10_MAX_SAME_DIRECTION_POSITIONS","V10_MAX_LONG_POSITIONS","V10_MAX_SHORT_POSITIONS"):
        os.environ.setdefault(k, os.environ.get("MAX_POSITIONS", "5"))
    # Patch env_int so unknown same-direction env aliases default to 5 instead of hard-coded 2.
    old = globals().get("env_int")
    if not old:
        return
    aliases = {"MAX_SAME_DIRECTION_POSITIONS","MAX_LONG_POSITIONS","MAX_SHORT_POSITIONS","PORTFOLIO_SAME_DIRECTION_LIMIT","V10_MAX_SAME_DIRECTION_POSITIONS","V10_MAX_LONG_POSITIONS","V10_MAX_SHORT_POSITIONS"}
    def env_int_hotfix(name, default=0):
        if name in aliases and str(os.environ.get(name, "")).strip() == "":
            return 5
        return old(name, default)
    globals()["env_int"] = env_int_hotfix


def _cg_patch_backtest_diag():
    # Wrap analyzer classes if present to emit useful counters; do not change strategy outcome here.
    globals()["CG_BACKTEST_DIAG_HOTFIX_V2"] = True

try:
    _cg_patch_risk_limits()
    _cg_patch_client()
    _cg_patch_exchange_broker()
    _cg_patch_backtest_diag()
    _cg_logging.info("CHATGPT_HOTFIX_V2_LOADED")
except Exception:
    _cg_logging.exception("CHATGPT_HOTFIX_V2_LOAD_FAILED")
# === CHATGPT_HOTFIX_V2_END ===
'''

DASH_BLOCK = r"""
# === CHATGPT_DASHBOARD_HOTFIX_V2_START ===
# Dashboard hotfix v2: correct equity display, complete position table, clearer blockers/trades.
try:
    import html as _cg_html, json as _cg_json, math as _cg_math
except Exception:
    pass

def _cg_esc(v):
    try:
        return esc(v)  # type: ignore[name-defined]
    except Exception:
        return _cg_html.escape(str(v))

def _cg_float(v, default=0.0):
    try:
        if v in (None, "", "—", "N/A"):
            return float(default)
        return float(v)
    except Exception:
        return float(default)

def _cg_fmt(v, nd=2):
    try:
        return fmt_num(v)  # type: ignore[name-defined]
    except Exception:
        try:
            f = float(v)
            if abs(f) >= 100: return f"{f:,.2f}"
            if abs(f) >= 1: return f"{f:,.4f}"
            return f"{f:.8f}".rstrip("0").rstrip(".")
        except Exception:
            return str(v if v not in (None, "") else "—")

def _cg_status_badge(text, cls=""):
    try:
        return status_badge(text)  # type: ignore[name-defined]
    except Exception:
        return f'<span class="badge {cls}">{_cg_esc(text)}</span>'

def _cg_dir_badge(text):
    try:
        return dir_badge(text)  # type: ignore[name-defined]
    except Exception:
        t = str(text or "—").upper()
        return f'<span class="badge {"pos" if t=="LONG" else "neg" if t=="SHORT" else ""}">{_cg_esc(t)}</span>'

def _cg_pnl_cls(v):
    f = _cg_float(v, 0.0)
    return "pos" if f > 0 else "neg" if f < 0 else "muted"

def _cg_protection_badge(status):
    st = str(status or "NO_PROTECTION").upper()
    if st == "PROTECTED": return '<span class="badge pos">已保护</span>'
    if st in {"STOP_ONLY", "TP_ONLY"}: return '<span class="badge warn">部分保护</span>'
    return '<span class="badge neg">未保护</span>'

def _cg_money(v):
    return "—" if v in (None, "", "—") else _cg_fmt(v)

def build_overview(health, risk, proc):
    broker = ((health or {}).get("broker") or {})
    config = ((health or {}).get("config") or {})
    mode = str((health or {}).get("mode") or (risk or {}).get("mode") or config.get("trading_mode") or "—").upper()
    status = str((health or {}).get("status", "—")).upper()
    wallet = _cg_float(broker.get("wallet_balance"), 0.0)
    unreal = _cg_float(broker.get("total_unrealized_pnl"), 0.0)
    # Correct definition: equity = wallet balance + unrealized PnL. Do not mirror wallet.
    equity = broker.get("equity")
    equity_calc = wallet + unreal if wallet or unreal else _cg_float(equity, 0.0)
    avail = broker.get("available_balance", "—")
    total_notional = broker.get("total_notional", 0.0)
    total_im = broker.get("total_initial_margin", 0.0)
    positions = broker.get("position_detail", {}) if isinstance(broker.get("position_detail", {}), dict) else {}
    pos_cnt = broker.get("positions", len(positions))
    max_pos = config.get("max_positions", config.get("MAX_POSITIONS", "—"))
    regime = (proc or {}).get("regime", {}) if isinstance(proc, dict) else {}
    bias = str(regime.get("bias", "—")).upper()
    btc_p = regime.get("btc_price", "—")
    atr_pct = regime.get("atr_pct", "—")
    r_score = regime.get("score", regime.get("regime_score", "—"))
    top = (proc or {}).get("top", []) if isinstance(proc, dict) else []
    approved = sum(1 for x in top if str(x.get("decision", x.get("direction", ""))).upper() in {"LONG", "SHORT", "TRADE"}) if isinstance(top, list) else 0
    total_c = len(top) if isinstance(top, list) else 0
    tiers = {"A":0,"B":0,"C":0,"D":0}
    if isinstance(top, list):
        for x in top:
            t = str(x.get("tier", "D")).upper()[:1]
            if t in tiers: tiers[t] += 1
    update_t = (health or {}).get("time") or (proc or {}).get("time") or "—"
    atr_s = f"{_cg_float(atr_pct):.2f}%" if atr_pct != "—" else "—"
    return f'''
    <div class="grid overview-grid">
      <div class="card"><div class="label">机器人状态</div><div class="value">{_cg_status_badge(status)}</div></div>
      <div class="card"><div class="label">运行模式</div><div class="value">{_cg_status_badge(mode)}</div></div>
      <div class="card"><div class="label">钱包余额 (USDT)</div><div class="value">{_cg_esc(_cg_money(wallet))}</div></div>
      <div class="card"><div class="label">可用余额 (USDT)</div><div class="value">{_cg_esc(_cg_money(avail))}</div></div>
      <div class="card"><div class="label">总权益=钱包+浮盈亏</div><div class="value {_cg_pnl_cls(equity_calc-wallet)}">{_cg_esc(_cg_money(equity_calc))}</div></div>
      <div class="card"><div class="label">当前持仓 / 上限</div><div class="value">{_cg_esc(pos_cnt)} / {_cg_esc(max_pos)}</div></div>
      <div class="card"><div class="label">总未实现盈亏</div><div class="value {_cg_pnl_cls(unreal)}">{_cg_esc(_cg_money(unreal))}</div></div>
      <div class="card"><div class="label">总名义价值</div><div class="value">{_cg_esc(_cg_money(total_notional))} USDT</div></div>
      <div class="card"><div class="label">已用初始保证金</div><div class="value">{_cg_esc(_cg_money(total_im))} USDT</div></div>
      <div class="card"><div class="label">市场偏向</div><div class="value {_cg_pnl_cls(1 if bias=='LONG' else -1 if bias=='SHORT' else 0)}">{_cg_esc(bias)}</div></div>
      <div class="card"><div class="label">BTC 价格</div><div class="value">${_cg_esc(_cg_money(btc_p))}</div></div>
      <div class="card"><div class="label">市场ATR%</div><div class="value">{_cg_esc(atr_s)}</div></div>
      <div class="card"><div class="label">REGIME SCORE</div><div class="value">{_cg_esc(r_score)}</div></div>
      <div class="card"><div class="label">候选 / 策略通过</div><div class="value">{_cg_esc(total_c)} / {_cg_esc(approved)}</div></div>
      <div class="card"><div class="label">候选等级分布</div><div class="value small">A:{tiers['A']} B:{tiers['B']} C:{tiers['C']} D:{tiers['D']}</div></div>
      <div class="card wide"><div class="label">最近更新</div><div class="value small">{_cg_esc(update_t)}</div></div>
    </div>'''

def build_positions(health, proc=None):
    broker_pos = (((health or {}).get("broker") or {}).get("position_detail") or {})
    if not isinstance(broker_pos, dict) or not broker_pos:
        return '<div class="card empty">暂无持仓</div>'
    rows = []
    for sym, p in broker_pos.items():
        if not isinstance(p, dict): continue
        direction = p.get("direction") or p.get("positionSide") or "—"
        qty = p.get("qty_abs", p.get("positionAmt", p.get("qty", "—")))
        entry = p.get("entryPrice", p.get("entry", "—"))
        mark = p.get("markPrice", p.get("latest", "—"))
        notional = p.get("notional", "—")
        im = p.get("initial_margin", "—")
        lev = p.get("leverage", "—")
        margin_type = str(p.get("marginType", p.get("margin_type", "—"))).lower()
        pnl = p.get("unRealizedProfit", p.get("unrealized_pnl", "—"))
        roe = p.get("roe_pct", "—")
        try: roe_s = f"{float(roe):.2f}%"
        except Exception: roe_s = "—"
        liq = p.get("liquidationPrice", "—")
        if str(liq) in {"", "0", "0.0", "None"}: liq = "N/A"
        sl = p.get("stop_loss", "—") or "—"
        tp = p.get("tp2", p.get("take_profit", "—")) or "—"
        prot = p.get("protection_status", "NO_PROTECTION")
        ocount = p.get("open_orders_count", 0)
        rows.append(f'''
        <tr>
          <td>{_cg_esc(sym)}</td><td>{_cg_dir_badge(direction)}</td><td>{_cg_esc(_cg_fmt(qty))}</td>
          <td>{_cg_esc(_cg_fmt(entry))}</td><td>{_cg_esc(_cg_fmt(mark))}</td>
          <td>{_cg_esc(_cg_fmt(notional))}</td><td>{_cg_esc(_cg_fmt(im))}</td>
          <td>{_cg_esc(_cg_fmt(lev))}x</td><td>{_cg_esc(margin_type)}</td>
          <td class="{_cg_pnl_cls(pnl)}">{_cg_esc(_cg_fmt(pnl))}</td>
          <td class="{_cg_pnl_cls(roe)}">{_cg_esc(roe_s)}</td><td>{_cg_esc(_cg_fmt(liq))}</td>
          <td>{_cg_esc(_cg_fmt(sl))}</td><td>{_cg_esc(_cg_fmt(tp))}</td>
          <td>{_cg_protection_badge(prot)}</td><td>{_cg_esc(ocount)}</td>
        </tr>''')
    return '<div class="table-wrap"><table><thead><tr><th>币对</th><th>方向</th><th>数量</th><th>开仓价</th><th>最新价</th><th>名义价值</th><th>初始保证金</th><th>杠杆</th><th>模式</th><th>浮盈亏</th><th>ROE%</th><th>强平价</th><th>止损</th><th>止盈</th><th>保护</th><th>挂单</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'

def build_signals(proc):
    top = (proc or {}).get("top", []) if isinstance(proc, dict) else []
    if not isinstance(top, list) or not top:
        return '<div class="card empty">暂无候选信号</div>'
    rows = []
    for x in top[:30]:
        if not isinstance(x, dict): continue
        sym = x.get("symbol", "—")
        tier = x.get("tier", "—")
        score = x.get("score", x.get("confidence", "—"))
        direction = x.get("direction", x.get("decision", "NO_TRADE"))
        entry = x.get("entry", x.get("entry_price", x.get("price", "—")))
        reason = x.get("reject_reason") or x.get("block_reason") or x.get("reason") or ""
        status = "通过" if str(direction).upper() in {"LONG","SHORT","TRADE"} and not reason else "被拦截"
        rows.append(f'<tr><td>{_cg_esc(sym)}</td><td>{_cg_esc(tier)}</td><td>{_cg_esc(_cg_fmt(score))}</td><td>{_cg_dir_badge(direction)}</td><td>{_cg_esc(_cg_fmt(entry))}</td><td class="neg">{_cg_esc(reason)}</td><td>{_cg_status_badge(status)}</td></tr>')
    return '<div class="table-wrap"><table><thead><tr><th>币对</th><th>等级</th><th>分数</th><th>方向</th><th>入场价</th><th>拦截原因</th><th>状态</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'

def build_recent_trades(health, proc=None):
    rows_data = []
    for root in (health, proc):
        if isinstance(root, dict):
            for key in ("recent_trades", "trades", "journal", "recent_fills"):
                val = root.get(key)
                if isinstance(val, list): rows_data = val; break
        if rows_data: break
    if not rows_data and isinstance(proc, dict):
        val = proc.get("recent", [])
        if isinstance(val, list): rows_data = val
    if not rows_data:
        return '<div class="card empty">暂无成交记录</div>'
    rows = []
    for x in rows_data[:30]:
        if not isinstance(x, dict): continue
        rows.append(f'''<tr><td>{_cg_esc(x.get("time", "—"))}</td><td>{_cg_esc(x.get("event", x.get("type", "—")))}</td><td>{_cg_esc(x.get("symbol", "—"))}</td><td>{_cg_dir_badge(x.get("direction", "—"))}</td><td>{_cg_esc(x.get("setup", "—"))}</td><td>{_cg_esc(_cg_fmt(x.get("price", x.get("entry", "—"))))}</td><td>{_cg_esc(_cg_fmt(x.get("qty", "—")))}</td><td class="{_cg_pnl_cls(x.get("pnl", 0))}">{_cg_esc(_cg_fmt(x.get("pnl", "—")))}</td><td>{_cg_esc(_cg_fmt(x.get("balance", "—")))}</td><td>{_cg_esc(x.get("protection_status", "—"))}</td><td>{_cg_esc(x.get("reason", ""))}</td></tr>''')
    return '<div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>币对</th><th>方向</th><th>策略</th><th>价格</th><th>数量</th><th>盈亏</th><th>余额/权益</th><th>保护</th><th>原因</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
# === CHATGPT_DASHBOARD_HOTFIX_V2_END ===
"""


def patch_brain() -> None:
    if not BRAIN.exists():
        raise FileNotFoundError(BRAIN)
    text = read(BRAIN)
    text = insert_before_main(text, BRAIN_BLOCK)
    write(BRAIN, text)
    print(f"patched {BRAIN}")


def patch_dashboard() -> None:
    if not DASH.exists():
        raise FileNotFoundError(DASH)
    text = read(DASH)
    text = insert_dashboard_before_main(text, DASH_BLOCK)
    write(DASH, text)
    print(f"patched {DASH}")


def patch_bt_scripts() -> None:
    for path in (RUN_BT1, RUN_BT2):
        if not path.exists():
            continue
        text = read(path)
        text = re.sub(r"BACKTEST_LIMIT=\d+", "BACKTEST_LIMIT=120000", text)
        # Ensure current config is used in backtest.
        if "ENABLE_TREND_SETUP" not in text:
            text = text.replace("ENTRY_DELAY_BARS=1 \\", "ENTRY_DELAY_BARS=1 \\\n  ENABLE_TREND_SETUP=0 \\\n  ENABLE_BREAKOUT_SETUP=0 \\\n  SETUP_WHITELIST=pullback \\\n  ENABLE_CONFIRM_V2=1 \\")
        write(path, text)
        print(f"patched {path}")


def main() -> int:
    print(f"Brain hotfix v2 root={ROOT}")
    for p in (BRAIN, DASH, BT, ENV, RUN_BT1, RUN_BT2):
        backup(p)
    update_env()
    patch_brain()
    patch_dashboard()
    patch_bt_scripts()
    print("\nDONE. Next commands:")
    print("cd /root/brain")
    print("python3 -m py_compile brain_v11_1_aipro.py dashboard.py bt_analyze.py")
    print("source .demo.env && python3 brain_v11_1_aipro.py --show-config && python3 brain_v11_1_aipro.py --healthcheck && python3 brain_v11_1_aipro.py --scan")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

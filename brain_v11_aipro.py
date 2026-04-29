#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brain V11 / Institutional AI Quant Final OS - Binance USDT-M Futures Quant Framework

目标：把“自动扫描 + 自动合约交易机器人”升级为可研究、可回测、可验证、可复盘的正期望框架。

默认安全：TRADING_MODE=paper，本地虚拟盘；不会真实下单。
实盘需要同时设置 TRADING_MODE=live, LIVE_TRADING_CONFIRM=YES, USE_EXCHANGE_PROTECTION=1。

核心能力：
  - 自动扫描 Binance USDT-M PERPETUAL 市场
  - 多周期趋势/回踩/突破评分，输出 LONG / SHORT / NO_TRADE
  - ATR + 结构止损、分批止盈、移动止损
  - Paper 执行与持仓管理
  - 无未来函数回测：信号基于已收盘 K 线，下一根开盘入场
  - Walk-forward 参数验证：训练段选参数，测试段验证正期望
  - 交易日志、绩效指标、复盘报告

免责声明：本代码是研究和工程框架，不保证盈利。合约交易高风险，实盘前必须长期 paper/demo 与小资金验证。
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import hmac
import itertools
import json
import logging
import math
import os
import statistics
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 requests：请先运行 pip install requests") from exc

UTC = dt.timezone.utc


# =============================================================================
# 通用工具
# =============================================================================


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_day() -> str:
    return utc_now().strftime("%Y-%m-%d")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def pct(part: float, whole: float) -> float:
    if whole == 0:
        return 0.0
    return part / whole * 100.0


def parse_csv_symbols(raw: str) -> List[str]:
    out: List[str] = []
    for item in raw.replace(";", ",").split(","):
        s = item.strip().upper()
        if s:
            out.append(s)
    return out


def fmt_num(x: float, digits: int = 4) -> str:
    if not math.isfinite(x):
        return "nan"
    if abs(x) >= 1000:
        return f"{x:.2f}"
    if abs(x) >= 1:
        return f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    if unit == "m":
        return n * 60_000
    if unit == "h":
        return n * 60 * 60_000
    if unit == "d":
        return n * 24 * 60 * 60_000
    if unit == "w":
        return n * 7 * 24 * 60 * 60_000
    raise ValueError(f"不支持 interval: {interval}")


def iso_to_ms(raw: str) -> Optional[int]:
    if not raw:
        return None
    text = raw.strip()
    try:
        if text.isdigit():
            return int(text)
        if len(text) == 10:
            d = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
        return int(d.timestamp() * 1000)
    except Exception:
        raise ValueError(f"无法解析日期: {raw}")


def ms_to_iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def decimal_floor(value: float, step: str) -> str:
    try:
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        if d_step <= 0:
            return format(d_value.normalize(), "f")
        units = (d_value / d_step).to_integral_value(rounding=ROUND_DOWN)
        result = units * d_step
        return format(result.normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


class JsonStore:
    def __init__(self, path: Path, default: Dict[str, Any]):
        self.path = path
        self.default = default
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                merged = json.loads(json.dumps(self.default))
                merged.update(data)
                return merged
        except Exception:
            logging.exception("读取状态失败，使用默认状态: %s", self.path)
        return json.loads(json.dumps(self.default))

    def save(self, data: Dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)


# =============================================================================
# 配置
# =============================================================================


@dataclass(frozen=True)
class Settings:
    trading_mode: str
    base_url: str
    api_key: str
    secret: str
    hedge_mode: bool
    use_exchange_protection: bool

    workspace: Path
    state_file: Path
    journal_file: Path
    metrics_file: Path
    backtest_file: Path
    log_file: Path

    symbols: List[str]
    exclude_symbols: List[str]
    top_n: int
    min_quote_volume: float
    max_symbols_per_cycle: int

    entry_interval: str
    trend_interval: str
    regime_interval: str
    kline_limit: int
    loop_seconds: int

    score_threshold: float
    min_rr: float
    atr_sl_mult: float
    structure_sl_buffer_atr: float
    tp1_r: float
    tp2_r: float
    tp1_pct: float
    trailing_atr_mult: float
    min_atr_pct: float
    max_atr_pct: float
    min_stop_atr: float
    max_stop_atr: float
    max_funding_abs: float
    prefer_market_bias: bool
    min_volume_ratio: float
    max_hold_bars: int
    conservative_intrabar: bool

    leverage: int
    paper_start_balance: float
    risk_per_trade: float
    max_positions: int
    max_new_entries_per_cycle: int
    max_notional_pct_per_trade: float
    max_total_notional_pct: float
    daily_max_loss_pct: float
    cooldown_minutes: int
    fee_bps: float
    slippage_bps: float

    backtest_start: str
    backtest_end: str
    backtest_limit: int
    train_frac: float
    min_trades_for_param: int

    request_timeout: int
    recv_window: int

    @staticmethod
    def load() -> "Settings":
        mode_raw = env_str("TRADING_MODE", env_str("PAPER_TRADE", "paper")).lower()
        if mode_raw in {"real", "live", "false", "0"}:
            mode = "live"
        elif mode_raw in {"demo", "testnet"}:
            mode = "demo"
        else:
            mode = "paper"

        live_base = env_str("BINANCE_LIVE_BASE", "https://fapi.binance.com")
        demo_base = env_str("BINANCE_DEMO_BASE", "https://demo-fapi.binance.com")
        if mode == "live":
            base_url = live_base
            api_key = env_str("BINANCE_LIVE_API_KEY", env_str("BINANCE_API_KEY", ""))
            secret = env_str("BINANCE_LIVE_SECRET", env_str("BINANCE_SECRET", ""))
            if env_str("LIVE_TRADING_CONFIRM", "").upper() != "YES":
                raise SystemExit("拒绝启动 live：必须显式设置 LIVE_TRADING_CONFIRM=YES")
            if not env_bool("USE_EXCHANGE_PROTECTION", False):
                raise SystemExit("拒绝启动 live：必须设置 USE_EXCHANGE_PROTECTION=1，避免无交易所止损裸奔")
        elif mode == "demo":
            base_url = demo_base
            api_key = env_str("BINANCE_DEMO_API_KEY", env_str("BINANCE_API_KEY", ""))
            secret = env_str("BINANCE_DEMO_SECRET", env_str("BINANCE_SECRET", ""))
        else:
            base_url = env_str("BINANCE_PUBLIC_BASE", live_base)
            api_key = ""
            secret = ""

        if mode in {"demo", "live"} and (not api_key or not secret):
            raise SystemExit(f"{mode} 模式需要 API Key/Secret，请用环境变量配置，不要写进代码。")

        workspace = Path(env_str("BRAIN_WS", "./brain_v11_data")).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        symbols = parse_csv_symbols(env_str("SYMBOLS", ""))
        exclude = parse_csv_symbols(env_str("EXCLUDE_SYMBOLS", "USDCUSDT,BUSDUSDT,TUSDUSDT,FDUSDUSDT,BTCDOMUSDT"))

        return Settings(
            trading_mode=mode,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            secret=secret,
            hedge_mode=env_bool("BINANCE_HEDGE_MODE", False),
            use_exchange_protection=env_bool("USE_EXCHANGE_PROTECTION", False),
            workspace=workspace,
            state_file=workspace / "paper_state.json",
            journal_file=workspace / "trades.csv",
            metrics_file=workspace / "metrics.json",
            backtest_file=workspace / "backtest_trades.csv",
            log_file=workspace / "brain_v11.log",
            symbols=symbols,
            exclude_symbols=exclude,
            top_n=env_int("TOP_N", 40),
            min_quote_volume=env_float("MIN_QUOTE_VOLUME", 25_000_000.0),
            max_symbols_per_cycle=env_int("MAX_SYMBOLS_PER_CYCLE", 40),
            entry_interval=env_str("ENTRY_INTERVAL", "15m"),
            trend_interval=env_str("TREND_INTERVAL", "1h"),
            regime_interval=env_str("REGIME_INTERVAL", "4h"),
            kline_limit=env_int("KLINE_LIMIT", 240),
            loop_seconds=env_int("LOOP_SECONDS", 180),
            score_threshold=env_float("SCORE_THRESHOLD", 78.0),
            min_rr=env_float("MIN_RR", 1.8),
            atr_sl_mult=env_float("ATR_SL_MULT", 1.55),
            structure_sl_buffer_atr=env_float("STRUCTURE_SL_BUFFER_ATR", 0.25),
            tp1_r=env_float("TP1_R", 1.0),
            tp2_r=env_float("TP2_R", 2.3),
            tp1_pct=env_float("TP1_PCT", 0.35),
            trailing_atr_mult=env_float("TRAILING_ATR_MULT", 2.1),
            min_atr_pct=env_float("MIN_ATR_PCT", 0.25),
            max_atr_pct=env_float("MAX_ATR_PCT", 6.0),
            min_stop_atr=env_float("MIN_STOP_ATR", 0.8),
            max_stop_atr=env_float("MAX_STOP_ATR", 4.0),
            max_funding_abs=env_float("MAX_FUNDING_ABS", 0.0012),
            prefer_market_bias=env_bool("PREFER_MARKET_BIAS", True),
            min_volume_ratio=env_float("MIN_VOLUME_RATIO", 1.05),
            max_hold_bars=env_int("MAX_HOLD_BARS", 96),
            conservative_intrabar=env_bool("CONSERVATIVE_INTRABAR", True),
            leverage=env_int("LEVERAGE", 3),
            paper_start_balance=env_float("PAPER_START_BALANCE", 1000.0),
            risk_per_trade=env_float("RISK_PER_TRADE", 0.005),
            max_positions=env_int("MAX_POSITIONS", 2),
            max_new_entries_per_cycle=env_int("MAX_NEW_ENTRIES_PER_CYCLE", 1),
            max_notional_pct_per_trade=env_float("MAX_NOTIONAL_PCT_PER_TRADE", 0.35),
            max_total_notional_pct=env_float("MAX_TOTAL_NOTIONAL_PCT", 0.75),
            daily_max_loss_pct=env_float("DAILY_MAX_LOSS_PCT", 0.03),
            cooldown_minutes=env_int("COOLDOWN_MINUTES", 180),
            fee_bps=env_float("FEE_BPS", 4.0),
            slippage_bps=env_float("SLIPPAGE_BPS", 2.0),
            backtest_start=env_str("BACKTEST_START", ""),
            backtest_end=env_str("BACKTEST_END", ""),
            backtest_limit=env_int("BACKTEST_LIMIT", 1500),
            train_frac=env_float("TRAIN_FRAC", 0.65),
            min_trades_for_param=env_int("MIN_TRADES_FOR_PARAM", 20),
            request_timeout=env_int("REQUEST_TIMEOUT", 15),
            recv_window=env_int("RECV_WINDOW", 5000),
        )

    def sanitized(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data["api_key"] = "***" if self.api_key else ""
        data["secret"] = "***" if self.secret else ""
        for key in ["workspace", "state_file", "journal_file", "metrics_file", "backtest_file", "log_file"]:
            data[key] = str(data[key])
        return data


def setup_logging(settings: Settings) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout), logging.FileHandler(settings.log_file, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", handlers=handlers)


# =============================================================================
# 数据结构
# =============================================================================


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass
class SymbolRules:
    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    price_tick: str
    qty_step: str
    min_qty: float
    min_notional: float
    trigger_protect: float = 0.0

    def round_qty(self, qty: float) -> str:
        return decimal_floor(max(qty, 0.0), self.qty_step)

    def round_price(self, price: float) -> str:
        return decimal_floor(max(price, 0.0), self.price_tick)

    def qty_float(self, qty: float) -> float:
        return safe_float(self.round_qty(qty), 0.0)

    def price_float(self, price: float) -> float:
        return safe_float(self.round_price(price), 0.0)


@dataclass
class MarketRegime:
    bias: str  # LONG / SHORT / NEUTRAL / CHAOS
    score: float
    btc_price: float
    atr_pct: float
    reason: List[str]


@dataclass
class TradeSignal:
    symbol: str
    direction: str  # LONG / SHORT / NO_TRADE
    score: float
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    atr: float
    atr_pct: float
    rsi: float
    funding_rate: float
    notional_volume: float
    setup: str = ""
    reasons: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.direction in {"LONG", "SHORT"} and not self.blockers


@dataclass
class OrderPlan:
    symbol: str
    direction: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    qty: float
    notional: float
    risk_usdt: float
    score: float
    setup: str
    reasons: List[str]


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    setup: str
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    stop_loss: float
    tp1: float
    tp2: float
    r_multiple: float
    pnl_pct: float
    bars: int
    score: float
    reason: str


# =============================================================================
# Binance API 封装
# =============================================================================


class BinanceFuturesClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BrainV10/1.0"})
        if settings.api_key:
            self.session.headers.update({"X-MBX-APIKEY": settings.api_key})
        self._exchange_info_cache: Optional[Dict[str, Any]] = None
        self._rules_cache: Optional[Dict[str, SymbolRules]] = None

    def public(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.settings.base_url + path
        resp = self.session.request(method.upper(), url, params=params or {}, timeout=self.settings.request_timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text[:500]}")
        return resp.json()

    def signed(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.settings.api_key or not self.settings.secret:
            raise RuntimeError("signed endpoint requires API key/secret")
        p = dict(params or {})
        p["timestamp"] = now_ms()
        p["recvWindow"] = self.settings.recv_window
        query = urlencode(p, doseq=True)
        sig = hmac.new(self.settings.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = self.settings.base_url + path + "?" + query + "&signature=" + sig
        resp = self.session.request(method.upper(), url, timeout=self.settings.request_timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text[:500]}")
        return resp.json()

    def exchange_info(self) -> Dict[str, Any]:
        if self._exchange_info_cache is None:
            self._exchange_info_cache = self.public("GET", "/fapi/v1/exchangeInfo")
        return self._exchange_info_cache

    def symbol_rules(self) -> Dict[str, SymbolRules]:
        if self._rules_cache is not None:
            return self._rules_cache
        rules: Dict[str, SymbolRules] = {}
        for s in self.exchange_info().get("symbols", []):
            symbol = str(s.get("symbol", ""))
            filters = {f.get("filterType"): f for f in s.get("filters", []) if isinstance(f, dict)}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            min_notional_filter = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})
            min_notional = safe_float(min_notional_filter.get("notional", min_notional_filter.get("minNotional", 0.0)), 0.0)
            rules[symbol] = SymbolRules(
                symbol=symbol,
                status=str(s.get("status", "")),
                contract_type=str(s.get("contractType", "")),
                quote_asset=str(s.get("quoteAsset", "")),
                price_tick=str(price_filter.get("tickSize", "0.0001")),
                qty_step=str(lot_filter.get("stepSize", "0.001")),
                min_qty=safe_float(lot_filter.get("minQty"), 0.0),
                min_notional=min_notional,
                trigger_protect=safe_float(s.get("triggerProtect"), 0.0),
            )
        self._rules_cache = rules
        return rules

    def tickers_24h(self) -> List[Dict[str, Any]]:
        data = self.public("GET", "/fapi/v1/ticker/24hr")
        return data if isinstance(data, list) else [data]

    def klines(self, symbol: str, interval: str, limit: int = 500, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> List[Candle]:
        params: Dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": min(max(limit, 1), 1500)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        raw = self.public("GET", "/fapi/v1/klines", params)
        return parse_klines(raw)

    def historical_klines(self, symbol: str, interval: str, start_ms: Optional[int], end_ms: Optional[int], limit: int) -> List[Candle]:
        """分页获取历史 K 线。若未提供 start/end，则获取最近 limit 根。"""
        limit = max(1, min(limit, 50_000))
        if start_ms is None and end_ms is None:
            return self.klines(symbol, interval, min(limit, 1500))
        out: List[Candle] = []
        step = interval_to_ms(interval)
        cursor = start_ms
        while len(out) < limit:
            batch_limit = min(1500, limit - len(out))
            batch = self.klines(symbol, interval, batch_limit, cursor, end_ms)
            if not batch:
                break
            if out and batch[0].open_time <= out[-1].open_time:
                batch = [c for c in batch if c.open_time > out[-1].open_time]
            out.extend(batch)
            if len(batch) < batch_limit:
                break
            cursor = batch[-1].open_time + step
            if end_ms is not None and cursor >= end_ms:
                break
            time.sleep(0.03)
        return out

    def premium_index(self, symbol: str) -> Dict[str, Any]:
        try:
            data = self.public("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def account_balance_usdt(self) -> float:
        rows = self.signed("GET", "/fapi/v2/balance")
        for row in rows:
            if row.get("asset") == "USDT":
                return safe_float(row.get("balance"), 0.0)
        return 0.0

    def open_positions(self) -> List[Dict[str, Any]]:
        rows = self.signed("GET", "/fapi/v2/positionRisk")
        out = []
        for row in rows:
            amt = safe_float(row.get("positionAmt"), 0.0)
            if abs(amt) > 0:
                out.append(row)
        return out

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def market_order(self, symbol: str, direction: str, qty: str, reduce_only: bool = False) -> Dict[str, Any]:
        side = "BUY" if direction == "LONG" else "SELL"
        params: Dict[str, Any] = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty, "newOrderRespType": "RESULT"}
        if self.settings.hedge_mode:
            params["positionSide"] = direction
        elif reduce_only:
            params["reduceOnly"] = "true"
        return self.signed("POST", "/fapi/v1/order", params)

    def close_market_order(self, symbol: str, direction: str, qty: str) -> Dict[str, Any]:
        side = "SELL" if direction == "LONG" else "BUY"
        params: Dict[str, Any] = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty, "newOrderRespType": "RESULT"}
        if self.settings.hedge_mode:
            params["positionSide"] = direction
        else:
            params["reduceOnly"] = "true"
        return self.signed("POST", "/fapi/v1/order", params)

    def place_close_algo(self, symbol: str, direction: str, order_type: str, trigger_price: str) -> Dict[str, Any]:
        side = "SELL" if direction == "LONG" else "BUY"
        params: Dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": trigger_price,
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            "priceProtect": "TRUE",
            "newOrderRespType": "ACK",
        }
        if self.settings.hedge_mode:
            params["positionSide"] = direction
        return self.signed("POST", "/fapi/v1/algoOrder", params)


def parse_klines(raw: Sequence[Sequence[Any]]) -> List[Candle]:
    candles: List[Candle] = []
    for k in raw:
        try:
            candles.append(Candle(int(k[0]), safe_float(k[1]), safe_float(k[2]), safe_float(k[3]), safe_float(k[4]), safe_float(k[5]), int(k[6])))
        except Exception:
            continue
    return candles


# =============================================================================
# 指标
# =============================================================================


def closes(candles: Sequence[Candle]) -> List[float]:
    return [c.close for c in candles]


def highs(candles: Sequence[Candle]) -> List[float]:
    return [c.high for c in candles]


def lows(candles: Sequence[Candle]) -> List[float]:
    return [c.low for c in candles]


def volumes(candles: Sequence[Candle]) -> List[float]:
    return [c.volume for c in candles]


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def rolling_sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if not values or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = values[0]
    for i, v in enumerate(values):
        prev = alpha * v + (1.0 - alpha) * prev
        if i >= period - 1:
            out[i] = prev
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    es = ema_series(values, period)
    for x in reversed(es):
        if x is not None:
            return x
    return None


def rsi_series(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for idx in range(period + 1, len(values)):
        g = gains[idx - 1]
        l = losses[idx - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[idx] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    rs = rsi_series(values, period)
    for x in reversed(rs):
        if x is not None:
            return x
    return None


def atr_series(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs: List[float] = [0.0]
    for i in range(1, len(candles)):
        h = candles[i].high
        l = candles[i].low
        pc = candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    value = sum(trs[1 : period + 1]) / period
    out[period] = value
    for i in range(period + 1, len(candles)):
        value = (value * (period - 1) + trs[i]) / period
        out[i] = value
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    ats = atr_series(candles, period)
    for x in reversed(ats):
        if x is not None:
            return x
    return None


def bb_width(values: Sequence[float], period: int = 20, mult: float = 2.0) -> Optional[float]:
    if len(values) < period:
        return None
    window = list(values[-period:])
    mid = sum(window) / period
    if mid == 0:
        return None
    sd = statistics.pstdev(window)
    return ((mid + mult * sd) - (mid - mult * sd)) / mid


def slope(values: Sequence[float], period: int = 10) -> float:
    if len(values) < period + 1:
        return 0.0
    start = values[-period - 1]
    end = values[-1]
    if start == 0:
        return 0.0
    return (end - start) / start


def recent_swing_low(candles: Sequence[Candle], lookback: int = 12) -> float:
    return min((c.low for c in candles[-lookback:]), default=0.0)


def recent_swing_high(candles: Sequence[Candle], lookback: int = 12) -> float:
    return max((c.high for c in candles[-lookback:]), default=0.0)


def percentile(values: Sequence[float], p: float) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return 0.0
    k = (len(vals) - 1) * clamp(p, 0.0, 1.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


# =============================================================================
# 扫描 + 策略
# =============================================================================


class MarketScanner:
    def __init__(self, settings: Settings, client: BinanceFuturesClient):
        self.settings = settings
        self.client = client

    def tradable_symbols(self) -> List[Tuple[str, float]]:
        rules = self.client.symbol_rules()
        if self.settings.symbols:
            return [(s, 0.0) for s in self.settings.symbols if s in rules]
        excluded = set(self.settings.exclude_symbols)
        rows: List[Tuple[str, float]] = []
        for t in self.client.tickers_24h():
            sym = str(t.get("symbol", "")).upper()
            rule = rules.get(sym)
            if not rule or sym in excluded:
                continue
            if rule.status != "TRADING" or rule.contract_type != "PERPETUAL" or rule.quote_asset != "USDT":
                continue
            qv = safe_float(t.get("quoteVolume"), 0.0)
            if qv < self.settings.min_quote_volume:
                continue
            if sym.endswith("USDCUSDT") or sym.endswith("BUSDUSDT"):
                continue
            rows.append((sym, qv))
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[: self.settings.top_n]


class StrategyEngine:
    """多周期趋势/回踩/突破评分。所有判断只用已收盘 K 线。"""

    def __init__(self, settings: Settings, client: Optional[BinanceFuturesClient] = None):
        self.settings = settings
        self.client = client

    def analyze_regime_from_candles(self, c4: Sequence[Candle], c1: Optional[Sequence[Candle]] = None) -> MarketRegime:
        try:
            if len(c4) < 80:
                return MarketRegime("NEUTRAL", 50.0, c4[-1].close if c4 else 0.0, 0.0, ["BTC K线不足"])
            c1 = c1 or c4
            close4 = closes(c4)
            close1 = closes(c1)
            price = close4[-1]
            ema20_4 = ema(close4, 20) or price
            ema60_4 = ema(close4, 60) or price
            ema200_4 = ema(close4, 200) or ema60_4
            ema20_1 = ema(close1, 20) or close1[-1]
            ema60_1 = ema(close1, 60) or close1[-1]
            a = atr(c4, 14) or 0.0
            atrp = pct(a, price)
            reasons: List[str] = []
            if atrp > self.settings.max_atr_pct * 1.35:
                return MarketRegime("CHAOS", 0.0, price, atrp, [f"BTC {self.settings.regime_interval} ATR% 过高 {atrp:.2f}%"])
            if price > ema200_4 and ema20_4 > ema60_4 and ema20_1 > ema60_1:
                score = 75.0
                reasons.append("BTC 大级别多头排列")
                if slope(close4, 8) > 0:
                    score += 10
                    reasons.append("BTC 斜率向上")
                return MarketRegime("LONG", clamp(score, 0, 100), price, atrp, reasons)
            if price < ema200_4 and ema20_4 < ema60_4 and ema20_1 < ema60_1:
                score = 75.0
                reasons.append("BTC 大级别空头排列")
                if slope(close4, 8) < 0:
                    score += 10
                    reasons.append("BTC 斜率向下")
                return MarketRegime("SHORT", clamp(score, 0, 100), price, atrp, reasons)
            return MarketRegime("NEUTRAL", 50.0, price, atrp, ["BTC 方向不一致"])
        except Exception as exc:
            return MarketRegime("NEUTRAL", 50.0, 0.0, 0.0, [f"BTC regime error: {exc}"])

    def analyze_regime(self) -> MarketRegime:
        if not self.client:
            return MarketRegime("NEUTRAL", 50.0, 0.0, 0.0, ["无 client"])
        try:
            c4 = self.client.klines("BTCUSDT", self.settings.regime_interval, self.settings.kline_limit)
            c1 = self.client.klines("BTCUSDT", self.settings.trend_interval, self.settings.kline_limit)
            return self.analyze_regime_from_candles(c4, c1)
        except Exception as exc:
            logging.warning("市场环境分析失败: %s", exc)
            return MarketRegime("NEUTRAL", 50.0, 0.0, 0.0, ["BTC regime unavailable"])

    def analyze_symbol_from_candles(
        self,
        symbol: str,
        entry: Sequence[Candle],
        trend: Sequence[Candle],
        regime: MarketRegime,
        quote_volume: float = 0.0,
        funding_rate: float = 0.0,
    ) -> TradeSignal:
        blockers: List[str] = []
        reasons_long: List[str] = []
        reasons_short: List[str] = []
        score_long = 0.0
        score_short = 0.0
        try:
            if len(trend) < 100 or len(entry) < 80:
                return self._no_trade(symbol, quote_volume, ["K线数量不足"])
            c_trend = closes(trend)
            c_entry = closes(entry)
            v_entry = volumes(entry)
            price = c_entry[-1]
            a = atr(entry, 14) or 0.0
            if price <= 0 or a <= 0:
                return self._no_trade(symbol, quote_volume, ["价格或 ATR 无效"])
            atrp = pct(a, price)
            r = rsi(c_entry, 14) or 50.0
            ema20_t = ema(c_trend, 20) or price
            ema60_t = ema(c_trend, 60) or price
            ema200_t = ema(c_trend, 200) or ema60_t
            e20s = ema_series(c_entry, 20)
            e60s = ema_series(c_entry, 60)
            ema20_e = e20s[-1] or price
            ema60_e = e60s[-1] or price
            ema20_prev = e20s[-2] or ema20_e
            vol_ma = sma(v_entry, 20) or max(v_entry[-1], 1.0)
            vol_ratio = v_entry[-1] / vol_ma if vol_ma else 1.0
            bbw = bb_width(c_entry, 20) or 0.0
            last = entry[-1]
            prev = entry[-2]

            if atrp < self.settings.min_atr_pct:
                blockers.append(f"ATR% 过低 {atrp:.2f}%")
            if atrp > self.settings.max_atr_pct:
                blockers.append(f"ATR% 过高 {atrp:.2f}%")
            if abs(funding_rate) > self.settings.max_funding_abs:
                blockers.append(f"资金费率过热 {funding_rate:.5f}")
            if regime.bias == "CHAOS":
                blockers.append("BTC 环境混乱")

            # 市场环境
            if regime.bias == "LONG":
                score_long += 12
                reasons_long.append("BTC 环境偏多")
                if self.settings.prefer_market_bias:
                    score_short -= 8
            elif regime.bias == "SHORT":
                score_short += 12
                reasons_short.append("BTC 环境偏空")
                if self.settings.prefer_market_bias:
                    score_long -= 8
            else:
                score_long += 4
                score_short += 4

            # 趋势底层过滤
            trend_long = c_trend[-1] > ema200_t and ema20_t > ema60_t and slope(c_trend, 10) > -0.005
            trend_short = c_trend[-1] < ema200_t and ema20_t < ema60_t and slope(c_trend, 10) < 0.005
            if trend_long:
                score_long += 24
                reasons_long.append("1H 趋势多头")
            if trend_short:
                score_short += 24
                reasons_short.append("1H 趋势空头")

            # Setup A: 趋势回踩
            long_pullback = trend_long and prev.low <= ema20_prev * 1.004 and last.close > ema20_e and last.close > last.open
            short_pullback = trend_short and prev.high >= ema20_prev * 0.996 and last.close < ema20_e and last.close < last.open
            if long_pullback:
                score_long += 24
                reasons_long.append("15M 趋势回踩后转强")
            if short_pullback:
                score_short += 24
                reasons_short.append("15M 趋势反弹后转弱")

            # Setup B: 突破启动，不追极端，只在波动挤压/量能确认时加分
            look = entry[-24:-1]
            don_high = max(c.high for c in look) if look else last.high
            don_low = min(c.low for c in look) if look else last.low
            long_breakout = trend_long and last.close > don_high and vol_ratio >= max(self.settings.min_volume_ratio, 1.15)
            short_breakout = trend_short and last.close < don_low and vol_ratio >= max(self.settings.min_volume_ratio, 1.15)
            if long_breakout:
                score_long += 18
                reasons_long.append("15M 放量突破前高")
            if short_breakout:
                score_short += 18
                reasons_short.append("15M 放量跌破前低")

            if price > ema60_e and ema20_e > ema60_e:
                score_long += 10
                reasons_long.append("15M 均线多头")
            if price < ema60_e and ema20_e < ema60_e:
                score_short += 10
                reasons_short.append("15M 均线空头")

            if vol_ratio >= self.settings.min_volume_ratio and last.close > last.open:
                score_long += 8
                reasons_long.append(f"成交量确认 {vol_ratio:.2f}x")
            if vol_ratio >= self.settings.min_volume_ratio and last.close < last.open:
                score_short += 8
                reasons_short.append(f"成交量确认 {vol_ratio:.2f}x")

            if 42 <= r <= 68:
                score_long += 8
                reasons_long.append(f"RSI 多头区间 {r:.1f}")
            elif r > 74:
                score_long -= 14
                reasons_long.append(f"RSI 过热 {r:.1f}")
            if 32 <= r <= 58:
                score_short += 8
                reasons_short.append(f"RSI 空头区间 {r:.1f}")
            elif r < 26:
                score_short -= 14
                reasons_short.append(f"RSI 过冷 {r:.1f}")

            if 0 < bbw < 0.035 and vol_ratio >= 1.1:
                score_long += 4
                score_short += 4

            if funding_rate > 0.0006:
                score_long -= 5
                score_short += 3
            elif funding_rate < -0.0006:
                score_short -= 5
                score_long += 3

            long_sl_struct = recent_swing_low(entry, 14) - self.settings.structure_sl_buffer_atr * a
            short_sl_struct = recent_swing_high(entry, 14) + self.settings.structure_sl_buffer_atr * a
            long_sl_atr = price - self.settings.atr_sl_mult * a
            short_sl_atr = price + self.settings.atr_sl_mult * a
            long_sl = min(long_sl_struct, long_sl_atr)
            short_sl = max(short_sl_struct, short_sl_atr)
            long_stop_dist = price - long_sl
            short_stop_dist = short_sl - price
            long_stop_atr = long_stop_dist / a if a else 999
            short_stop_atr = short_stop_dist / a if a else 999

            if not (self.settings.min_stop_atr <= long_stop_atr <= self.settings.max_stop_atr):
                score_long -= 20
                reasons_long.append(f"多头止损距离不合适 {long_stop_atr:.2f} ATR")
            else:
                score_long += 8
            if not (self.settings.min_stop_atr <= short_stop_atr <= self.settings.max_stop_atr):
                score_short -= 20
                reasons_short.append(f"空头止损距离不合适 {short_stop_atr:.2f} ATR")
            else:
                score_short += 8

            rr = self.settings.tp2_r
            if rr >= self.settings.min_rr:
                score_long += 8
                score_short += 8
            else:
                blockers.append(f"RR 不足 {rr:.2f}")

            score_long = clamp(score_long, 0, 100)
            score_short = clamp(score_short, 0, 100)
            if score_long >= score_short:
                direction = "LONG"
                score = score_long
                sl = long_sl
                dist = long_stop_dist
                tp1 = price + self.settings.tp1_r * dist
                tp2 = price + self.settings.tp2_r * dist
                reasons = reasons_long
                setup = "pullback" if long_pullback else "breakout" if long_breakout else "trend"
            else:
                direction = "SHORT"
                score = score_short
                sl = short_sl
                dist = short_stop_dist
                tp1 = price - self.settings.tp1_r * dist
                tp2 = price - self.settings.tp2_r * dist
                reasons = reasons_short
                setup = "pullback" if short_pullback else "breakout" if short_breakout else "trend"

            local_blockers = list(blockers)
            if score < self.settings.score_threshold:
                local_blockers.append(f"评分不足 {score:.1f} < {self.settings.score_threshold:.1f}")
            if dist <= 0:
                local_blockers.append("止损距离无效")
            if regime.bias in {"LONG", "SHORT"} and self.settings.prefer_market_bias and direction != regime.bias and score < self.settings.score_threshold + 10:
                local_blockers.append(f"逆 BTC 环境交易，分数不够强：{direction} vs {regime.bias}")
            if setup == "trend" and score < self.settings.score_threshold + 8:
                local_blockers.append("缺少明确回踩/突破入场结构")

            return TradeSignal(symbol, direction if not local_blockers else "NO_TRADE", score, price, sl, tp1, tp2, rr, a, atrp, r, funding_rate, quote_volume, setup, reasons, local_blockers)
        except Exception as exc:
            return self._no_trade(symbol, quote_volume, [f"分析异常: {exc}"])

    def analyze_symbol(self, symbol: str, quote_volume: float, regime: MarketRegime) -> TradeSignal:
        if not self.client:
            return self._no_trade(symbol, quote_volume, ["无 client"])
        try:
            trend = self.client.klines(symbol, self.settings.trend_interval, self.settings.kline_limit)
            entry = self.client.klines(symbol, self.settings.entry_interval, self.settings.kline_limit)
            fund = safe_float(self.client.premium_index(symbol).get("lastFundingRate"), 0.0)
            return self.analyze_symbol_from_candles(symbol, entry, trend, regime, quote_volume, fund)
        except Exception as exc:
            logging.debug("%s 分析失败: %s", symbol, exc)
            return self._no_trade(symbol, quote_volume, [f"分析异常: {exc}"])

    def _no_trade(self, symbol: str, quote_volume: float, blockers: List[str]) -> TradeSignal:
        return TradeSignal(symbol, "NO_TRADE", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 50.0, 0.0, quote_volume, "", [], blockers)


# =============================================================================
# 风控、日志、执行
# =============================================================================


class TradeJournal:
    FIELDS = ["time", "event", "symbol", "direction", "setup", "qty", "price", "entry", "stop_loss", "tp1", "tp2", "score", "risk_usdt", "pnl", "balance", "reason"]

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, row: Dict[str, Any]) -> None:
        data = {k: row.get(k, "") for k in self.FIELDS}
        data.setdefault("time", utc_now().isoformat())
        with self.path.open("a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow(data)


class RiskManager:
    def __init__(self, settings: Settings, rules: Dict[str, SymbolRules]):
        self.settings = settings
        self.rules = rules

    def build_plan(self, signal: TradeSignal, equity: float, open_positions: Dict[str, Any], total_notional: float, cooldowns: Dict[str, str]) -> Tuple[Optional[OrderPlan], List[str]]:
        blockers: List[str] = []
        if not signal.allowed:
            return None, signal.blockers
        if signal.symbol in open_positions:
            blockers.append("已有持仓")
        if len(open_positions) >= self.settings.max_positions:
            blockers.append("持仓数量达到上限")
        if self.in_cooldown(signal.symbol, cooldowns):
            blockers.append("冷却期内")
        if equity <= 0:
            blockers.append("权益无效")
        stop_dist = abs(signal.entry - signal.stop_loss)
        if stop_dist <= 0:
            blockers.append("止损距离无效")
        rule = self.rules.get(signal.symbol)
        if not rule:
            blockers.append("缺少交易规则")
        if blockers:
            return None, blockers
        assert rule is not None
        risk_usdt = equity * self.settings.risk_per_trade
        raw_qty = risk_usdt / stop_dist
        max_notional = equity * self.settings.max_notional_pct_per_trade * self.settings.leverage
        raw_qty = min(raw_qty, max_notional / signal.entry)
        qty = rule.qty_float(raw_qty)
        notional = qty * signal.entry
        max_total_notional = equity * self.settings.max_total_notional_pct * self.settings.leverage
        if qty <= 0:
            blockers.append("数量取整后为 0")
        if qty < rule.min_qty:
            blockers.append(f"数量低于最小下单量 {rule.min_qty}")
        if rule.min_notional and notional < rule.min_notional:
            blockers.append(f"名义价值低于最小要求 {rule.min_notional}")
        if total_notional + notional > max_total_notional:
            blockers.append("总名义仓位达到上限")
        if blockers:
            return None, blockers
        return OrderPlan(signal.symbol, signal.direction, rule.price_float(signal.entry), rule.price_float(signal.stop_loss), rule.price_float(signal.tp1), rule.price_float(signal.tp2), qty, notional, risk_usdt, signal.score, signal.setup, signal.reasons), []

    def in_cooldown(self, symbol: str, cooldowns: Dict[str, str]) -> bool:
        raw = cooldowns.get(symbol)
        if not raw:
            return False
        try:
            until = dt.datetime.fromisoformat(raw)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            return utc_now() < until
        except Exception:
            return False


class PaperBroker:
    def __init__(self, settings: Settings, journal: TradeJournal):
        self.settings = settings
        self.journal = journal
        self.store = JsonStore(settings.state_file, {"balance": settings.paper_start_balance, "day": utc_day(), "day_start_equity": settings.paper_start_balance, "positions": {}, "cooldowns": {}, "closed_trades": []})
        self.state = self.store.load()
        self._roll_day_if_needed()

    def _save(self) -> None:
        self.store.save(self.state)

    def _roll_day_if_needed(self) -> None:
        day = utc_day()
        if self.state.get("day") != day:
            self.state["day"] = day
            self.state["day_start_equity"] = self.equity({})
            self._save()

    def positions(self) -> Dict[str, Any]:
        return dict(self.state.get("positions", {}))

    def cooldowns(self) -> Dict[str, str]:
        return dict(self.state.get("cooldowns", {}))

    def balance(self) -> float:
        return safe_float(self.state.get("balance"), self.settings.paper_start_balance)

    def equity(self, mark_prices: Dict[str, float]) -> float:
        eq = self.balance()
        for sym, pos in self.positions().items():
            mark = mark_prices.get(sym, safe_float(pos.get("entry"), 0.0))
            qty = safe_float(pos.get("qty_remaining"), safe_float(pos.get("qty"), 0.0))
            entry = safe_float(pos.get("entry"), 0.0)
            if pos.get("direction") == "LONG":
                eq += (mark - entry) * qty
            elif pos.get("direction") == "SHORT":
                eq += (entry - mark) * qty
        return eq

    def total_notional(self, mark_prices: Dict[str, float]) -> float:
        return sum(abs(mark_prices.get(sym, safe_float(pos.get("entry"), 0.0)) * safe_float(pos.get("qty_remaining"), 0.0)) for sym, pos in self.positions().items())

    def daily_loss_pct(self, mark_prices: Dict[str, float]) -> float:
        start = safe_float(self.state.get("day_start_equity"), self.settings.paper_start_balance)
        if start <= 0:
            return 0.0
        return max(0.0, (start - self.equity(mark_prices)) / start)

    def can_open_today(self, mark_prices: Dict[str, float]) -> Tuple[bool, str]:
        loss = self.daily_loss_pct(mark_prices)
        if loss >= self.settings.daily_max_loss_pct:
            return False, f"每日亏损熔断 {loss:.2%} >= {self.settings.daily_max_loss_pct:.2%}"
        return True, "OK"

    def open_position(self, plan: OrderPlan, signal: TradeSignal) -> None:
        positions = self.state.setdefault("positions", {})
        if plan.symbol in positions:
            raise RuntimeError("已有持仓")
        slip = self.settings.slippage_bps / 10000.0
        entry = plan.entry * (1 + slip if plan.direction == "LONG" else 1 - slip)
        fee = abs(entry * plan.qty) * self.settings.fee_bps / 10000.0
        self.state["balance"] = self.balance() - fee
        positions[plan.symbol] = {"symbol": plan.symbol, "direction": plan.direction, "setup": plan.setup, "qty": plan.qty, "qty_remaining": plan.qty, "entry": entry, "stop_loss": plan.stop_loss, "tp1": plan.tp1, "tp2": plan.tp2, "tp1_hit": False, "highest": entry, "lowest": entry, "atr": signal.atr, "score": plan.score, "risk_usdt": plan.risk_usdt, "opened_at": utc_now().isoformat(), "reasons": plan.reasons}
        self._save()
        self.journal.write({"time": utc_now().isoformat(), "event": "OPEN", "symbol": plan.symbol, "direction": plan.direction, "setup": plan.setup, "qty": plan.qty, "price": entry, "entry": entry, "stop_loss": plan.stop_loss, "tp1": plan.tp1, "tp2": plan.tp2, "score": plan.score, "risk_usdt": plan.risk_usdt, "pnl": -fee, "balance": self.balance(), "reason": " | ".join(plan.reasons[:6])})
        logging.info("PAPER 开仓 %s %s qty=%s entry=%s sl=%s tp2=%s score=%.1f", plan.symbol, plan.direction, fmt_num(plan.qty), fmt_num(entry), fmt_num(plan.stop_loss), fmt_num(plan.tp2), plan.score)

    def manage_positions(self, mark_prices: Dict[str, float]) -> None:
        positions = self.state.setdefault("positions", {})
        for sym in list(positions.keys()):
            pos = positions.get(sym, {})
            price = mark_prices.get(sym)
            if not price or price <= 0:
                continue
            direction = pos.get("direction")
            qty_remaining = safe_float(pos.get("qty_remaining"), 0.0)
            if qty_remaining <= 0:
                positions.pop(sym, None)
                continue
            atr_value = safe_float(pos.get("atr"), 0.0)
            entry = safe_float(pos.get("entry"), 0.0)
            stop = safe_float(pos.get("stop_loss"), 0.0)
            tp1 = safe_float(pos.get("tp1"), 0.0)
            tp2 = safe_float(pos.get("tp2"), 0.0)
            if direction == "LONG":
                pos["highest"] = max(safe_float(pos.get("highest"), entry), price)
                if safe_float(pos.get("highest"), entry) > entry and atr_value > 0:
                    pos["stop_loss"] = max(stop, safe_float(pos.get("highest"), entry) - self.settings.trailing_atr_mult * atr_value)
                    stop = safe_float(pos.get("stop_loss"), stop)
                if price <= stop:
                    self._close(sym, qty_remaining, stop, "STOP")
                    continue
                if not pos.get("tp1_hit") and price >= tp1:
                    qty_close = qty_remaining * clamp(self.settings.tp1_pct, 0.05, 0.95)
                    self._partial_close(sym, qty_close, tp1, "TP1")
                    if sym in positions:
                        positions[sym]["tp1_hit"] = True
                        positions[sym]["stop_loss"] = max(safe_float(positions[sym].get("stop_loss"), stop), entry)
                    continue
                if price >= tp2:
                    self._close(sym, safe_float(positions.get(sym, {}).get("qty_remaining"), qty_remaining), tp2, "TP2")
                    continue
            elif direction == "SHORT":
                pos["lowest"] = min(safe_float(pos.get("lowest"), entry), price)
                if safe_float(pos.get("lowest"), entry) < entry and atr_value > 0:
                    pos["stop_loss"] = min(stop, safe_float(pos.get("lowest"), entry) + self.settings.trailing_atr_mult * atr_value)
                    stop = safe_float(pos.get("stop_loss"), stop)
                if price >= stop:
                    self._close(sym, qty_remaining, stop, "STOP")
                    continue
                if not pos.get("tp1_hit") and price <= tp1:
                    qty_close = qty_remaining * clamp(self.settings.tp1_pct, 0.05, 0.95)
                    self._partial_close(sym, qty_close, tp1, "TP1")
                    if sym in positions:
                        positions[sym]["tp1_hit"] = True
                        positions[sym]["stop_loss"] = min(safe_float(positions[sym].get("stop_loss"), stop), entry)
                    continue
                if price <= tp2:
                    self._close(sym, safe_float(positions.get(sym, {}).get("qty_remaining"), qty_remaining), tp2, "TP2")
                    continue
        self._save()

    def _pnl(self, pos: Dict[str, Any], qty: float, exit_price: float) -> float:
        entry = safe_float(pos.get("entry"), 0.0)
        return (exit_price - entry) * qty if pos.get("direction") == "LONG" else (entry - exit_price) * qty

    def _partial_close(self, sym: str, qty: float, exit_price: float, event: str) -> None:
        positions = self.state.setdefault("positions", {})
        pos = positions.get(sym)
        if not pos:
            return
        qty = min(qty, safe_float(pos.get("qty_remaining"), 0.0))
        fee = abs(exit_price * qty) * self.settings.fee_bps / 10000.0
        pnl = self._pnl(pos, qty, exit_price) - fee
        self.state["balance"] = self.balance() + pnl
        pos["qty_remaining"] = max(0.0, safe_float(pos.get("qty_remaining"), 0.0) - qty)
        self._save()
        self.journal.write({"time": utc_now().isoformat(), "event": event, "symbol": sym, "direction": pos.get("direction"), "setup": pos.get("setup"), "qty": qty, "price": exit_price, "entry": pos.get("entry"), "stop_loss": pos.get("stop_loss"), "tp1": pos.get("tp1"), "tp2": pos.get("tp2"), "score": pos.get("score"), "risk_usdt": pos.get("risk_usdt"), "pnl": pnl, "balance": self.balance(), "reason": event})
        logging.info("PAPER 部分平仓 %s %s qty=%s price=%s pnl=%s", sym, event, fmt_num(qty), fmt_num(exit_price), fmt_num(pnl))

    def _close(self, sym: str, qty: float, exit_price: float, event: str) -> None:
        positions = self.state.setdefault("positions", {})
        pos = positions.get(sym)
        if not pos:
            return
        qty = min(qty, safe_float(pos.get("qty_remaining"), 0.0))
        fee = abs(exit_price * qty) * self.settings.fee_bps / 10000.0
        pnl = self._pnl(pos, qty, exit_price) - fee
        self.state["balance"] = self.balance() + pnl
        pos["qty_remaining"] = max(0.0, safe_float(pos.get("qty_remaining"), 0.0) - qty)
        closed = {"symbol": sym, "direction": pos.get("direction"), "setup": pos.get("setup"), "opened_at": pos.get("opened_at"), "closed_at": utc_now().isoformat(), "entry": pos.get("entry"), "exit": exit_price, "qty": qty, "pnl": pnl, "event": event, "score": pos.get("score")}
        self.state.setdefault("closed_trades", []).append(closed)
        self.state.setdefault("cooldowns", {})[sym] = (utc_now() + dt.timedelta(minutes=self.settings.cooldown_minutes)).isoformat()
        positions.pop(sym, None)
        self._save()
        self.journal.write({"time": utc_now().isoformat(), "event": event, "symbol": sym, "direction": pos.get("direction"), "setup": pos.get("setup"), "qty": qty, "price": exit_price, "entry": pos.get("entry"), "stop_loss": pos.get("stop_loss"), "tp1": pos.get("tp1"), "tp2": pos.get("tp2"), "score": pos.get("score"), "risk_usdt": pos.get("risk_usdt"), "pnl": pnl, "balance": self.balance(), "reason": event})
        logging.info("PAPER 平仓 %s %s qty=%s price=%s pnl=%s balance=%s", sym, event, fmt_num(qty), fmt_num(exit_price), fmt_num(pnl), fmt_num(self.balance()))


class ExchangeBroker:
    def __init__(self, settings: Settings, client: BinanceFuturesClient, journal: TradeJournal, rules: Dict[str, SymbolRules]):
        self.settings = settings
        self.client = client
        self.journal = journal
        self.rules = rules

    def positions(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for row in self.client.open_positions():
            sym = row.get("symbol")
            if sym:
                out[sym] = row
        return out

    def cooldowns(self) -> Dict[str, str]:
        return {}

    def equity(self, mark_prices: Dict[str, float]) -> float:
        return self.client.account_balance_usdt()

    def total_notional(self, mark_prices: Dict[str, float]) -> float:
        total = 0.0
        for row in self.client.open_positions():
            total += abs(safe_float(row.get("positionAmt"), 0.0) * safe_float(row.get("markPrice"), 0.0))
        return total

    def can_open_today(self, mark_prices: Dict[str, float]) -> Tuple[bool, str]:
        return True, "OK"

    def manage_positions(self, mark_prices: Dict[str, float]) -> None:
        return None

    def open_position(self, plan: OrderPlan, signal: TradeSignal) -> None:
        rule = self.rules[plan.symbol]
        qty_str = rule.round_qty(plan.qty)
        self.client.set_leverage(plan.symbol, self.settings.leverage)
        res = self.client.market_order(plan.symbol, plan.direction, qty_str)
        logging.info("%s 开仓返回: %s", self.settings.trading_mode.upper(), json.dumps(res, ensure_ascii=False)[:500])
        if self.settings.use_exchange_protection:
            sl_res = self.client.place_close_algo(plan.symbol, plan.direction, "STOP_MARKET", rule.round_price(plan.stop_loss))
            tp_res = self.client.place_close_algo(plan.symbol, plan.direction, "TAKE_PROFIT_MARKET", rule.round_price(plan.tp2))
            logging.info("保护单已提交 SL=%s TP=%s", sl_res, tp_res)
        else:
            logging.warning("未启用 USE_EXCHANGE_PROTECTION，demo/live 不会自动挂交易所保护单。")
        self.journal.write({"time": utc_now().isoformat(), "event": "EXCHANGE_OPEN", "symbol": plan.symbol, "direction": plan.direction, "setup": plan.setup, "qty": qty_str, "price": plan.entry, "entry": plan.entry, "stop_loss": plan.stop_loss, "tp1": plan.tp1, "tp2": plan.tp2, "score": plan.score, "risk_usdt": plan.risk_usdt, "reason": " | ".join(plan.reasons[:6])})


# =============================================================================
# 回测与验证
# =============================================================================


class Backtester:
    def __init__(self, settings: Settings, client: BinanceFuturesClient):
        self.settings = settings
        self.client = client
        self.strategy = StrategyEngine(settings, client=None)

    def backtest_symbol(self, symbol: str, quote_volume: float = 0.0, settings_override: Optional[Settings] = None) -> List[BacktestTrade]:
        st = settings_override or self.settings
        engine = StrategyEngine(st, client=None)
        start_ms = iso_to_ms(st.backtest_start) if st.backtest_start else None
        end_ms = iso_to_ms(st.backtest_end) if st.backtest_end else None
        entry = self.client.historical_klines(symbol, st.entry_interval, start_ms, end_ms, st.backtest_limit)
        trend = self.client.historical_klines(symbol, st.trend_interval, start_ms, end_ms, max(500, st.backtest_limit // 4 + 300))
        btc4 = self.client.historical_klines("BTCUSDT", st.regime_interval, start_ms, end_ms, max(500, st.backtest_limit // 16 + 300))
        btc1 = self.client.historical_klines("BTCUSDT", st.trend_interval, start_ms, end_ms, max(500, st.backtest_limit // 4 + 300))
        if len(entry) < 250 or len(trend) < 150:
            return []
        trades: List[BacktestTrade] = []
        open_until = -1
        for i in range(220, len(entry) - 2):
            if i <= open_until:
                continue
            signal_entry = entry[: i + 1]
            now_t = entry[i].close_time
            trend_slice = slice_until_close(trend, now_t)
            btc4_slice = slice_until_close(btc4, now_t)
            btc1_slice = slice_until_close(btc1, now_t)
            if len(trend_slice) < 100 or len(btc4_slice) < 80:
                continue
            regime = engine.analyze_regime_from_candles(btc4_slice, btc1_slice)
            sig = engine.analyze_symbol_from_candles(symbol, signal_entry, trend_slice, regime, quote_volume, 0.0)
            if not sig.allowed:
                continue
            trade = self._simulate_trade(symbol, sig, entry, i + 1, st)
            if trade:
                trades.append(trade)
                open_until = min(i + 1 + trade.bars, len(entry) - 1)
        return trades

    def _simulate_trade(self, symbol: str, sig: TradeSignal, candles: Sequence[Candle], entry_i: int, st: Settings) -> Optional[BacktestTrade]:
        if entry_i >= len(candles):
            return None
        direction = sig.direction
        entry_bar = candles[entry_i]
        slip = st.slippage_bps / 10000.0
        fee_r = st.fee_bps / 10000.0
        entry_price = entry_bar.open * (1 + slip if direction == "LONG" else 1 - slip)
        stop = sig.stop_loss
        risk = abs(entry_price - stop)
        if risk <= 0:
            return None
        # 按入场价重新推导 TP，避免信号收盘价与下一根开盘价存在缺口
        if direction == "LONG":
            tp1 = entry_price + st.tp1_r * risk
            tp2 = entry_price + st.tp2_r * risk
        else:
            tp1 = entry_price - st.tp1_r * risk
            tp2 = entry_price - st.tp2_r * risk
        qty_left = 1.0
        realized_r = 0.0
        tp1_hit = False
        highest = entry_price
        lowest = entry_price
        current_stop = stop
        exit_price = entry_price
        exit_t = entry_bar.close_time
        exit_reason = "MAX_HOLD"
        max_i = min(len(candles) - 1, entry_i + st.max_hold_bars)
        for j in range(entry_i, max_i + 1):
            bar = candles[j]
            exit_t = bar.close_time
            if direction == "LONG":
                highest = max(highest, bar.high)
                if highest > entry_price and sig.atr > 0:
                    current_stop = max(current_stop, highest - st.trailing_atr_mult * sig.atr)
                stop_hit = bar.low <= current_stop
                tp2_hit = bar.high >= tp2
                tp1_bar_hit = (not tp1_hit) and bar.high >= tp1
                if st.conservative_intrabar and stop_hit:
                    exit_price = current_stop * (1 - slip)
                    realized_r += qty_left * ((exit_price - entry_price) / risk)
                    exit_reason = "STOP"
                    break
                if tp1_bar_hit:
                    close_qty = qty_left * clamp(st.tp1_pct, 0.05, 0.95)
                    realized_r += close_qty * ((tp1 - entry_price) / risk)
                    qty_left -= close_qty
                    tp1_hit = True
                    current_stop = max(current_stop, entry_price)
                if tp2_hit:
                    exit_price = tp2 * (1 - slip)
                    realized_r += qty_left * ((exit_price - entry_price) / risk)
                    exit_reason = "TP2"
                    break
                if (not st.conservative_intrabar) and stop_hit:
                    exit_price = current_stop * (1 - slip)
                    realized_r += qty_left * ((exit_price - entry_price) / risk)
                    exit_reason = "STOP"
                    break
            else:
                lowest = min(lowest, bar.low)
                if lowest < entry_price and sig.atr > 0:
                    current_stop = min(current_stop, lowest + st.trailing_atr_mult * sig.atr)
                stop_hit = bar.high >= current_stop
                tp2_hit = bar.low <= tp2
                tp1_bar_hit = (not tp1_hit) and bar.low <= tp1
                if st.conservative_intrabar and stop_hit:
                    exit_price = current_stop * (1 + slip)
                    realized_r += qty_left * ((entry_price - exit_price) / risk)
                    exit_reason = "STOP"
                    break
                if tp1_bar_hit:
                    close_qty = qty_left * clamp(st.tp1_pct, 0.05, 0.95)
                    realized_r += close_qty * ((entry_price - tp1) / risk)
                    qty_left -= close_qty
                    tp1_hit = True
                    current_stop = min(current_stop, entry_price)
                if tp2_hit:
                    exit_price = tp2 * (1 + slip)
                    realized_r += qty_left * ((entry_price - exit_price) / risk)
                    exit_reason = "TP2"
                    break
                if (not st.conservative_intrabar) and stop_hit:
                    exit_price = current_stop * (1 + slip)
                    realized_r += qty_left * ((entry_price - exit_price) / risk)
                    exit_reason = "STOP"
                    break
        else:
            last = candles[max_i]
            exit_price = last.close * (1 - slip if direction == "LONG" else 1 + slip)
            realized_r += qty_left * (((exit_price - entry_price) / risk) if direction == "LONG" else ((entry_price - exit_price) / risk))

        # 粗略费用：入场 + 出场/分批，折算 R，按风险距离和 entry 粗估
        fee_r_total = ((entry_price * fee_r) + (exit_price * fee_r)) / risk
        realized_r -= fee_r_total
        pnl_pct = realized_r * (risk / entry_price) * 100.0
        bars = max(1, int((exit_t - entry_bar.open_time) / interval_to_ms(st.entry_interval)))
        return BacktestTrade(symbol, direction, sig.setup, entry_bar.open_time, exit_t, entry_price, exit_price, stop, tp1, tp2, realized_r, pnl_pct, bars, sig.score, exit_reason + " | " + " | ".join(sig.reasons[:4]))

    def backtest_many(self, symbols: Sequence[Tuple[str, float]], settings_override: Optional[Settings] = None) -> List[BacktestTrade]:
        all_trades: List[BacktestTrade] = []
        for idx, (sym, qv) in enumerate(symbols, start=1):
            logging.info("回测 %s/%s %s", idx, len(symbols), sym)
            try:
                all_trades.extend(self.backtest_symbol(sym, qv, settings_override))
            except Exception:
                logging.error("回测失败 %s\n%s", sym, traceback.format_exc())
        all_trades.sort(key=lambda t: t.entry_time)
        return all_trades

    def walk_forward_optimize(self, symbols: Sequence[Tuple[str, float]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        grid = list(itertools.product([72.0, 76.0, 80.0, 84.0], [1.25, 1.55, 1.9], [1.8, 2.3, 2.8], [0.3, 0.45]))
        results: List[Dict[str, Any]] = []
        for n, (threshold, atr_mult, tp2_r, tp1_pct) in enumerate(grid, start=1):
            st = replace(self.settings, score_threshold=threshold, atr_sl_mult=atr_mult, tp2_r=tp2_r, min_rr=min(self.settings.min_rr, tp2_r), tp1_pct=tp1_pct)
            logging.info("参数验证 %s/%s threshold=%s atr=%s tp2=%s tp1pct=%s", n, len(grid), threshold, atr_mult, tp2_r, tp1_pct)
            trades = self.backtest_many(symbols, st)
            train, test = split_trades_by_time(trades, st.train_frac)
            m_train = compute_metrics(train)
            m_test = compute_metrics(test)
            score = robust_param_score(m_train, m_test, st.min_trades_for_param)
            results.append({"score": score, "params": {"SCORE_THRESHOLD": threshold, "ATR_SL_MULT": atr_mult, "TP2_R": tp2_r, "TP1_PCT": tp1_pct}, "train": m_train, "test": m_test})
        results.sort(key=lambda x: x["score"], reverse=True)
        return (results[0] if results else {}), results


def slice_until_close(candles: Sequence[Candle], close_time: int) -> List[Candle]:
    # 简单线性，数据不大；需要极限性能时可改 bisect。
    out: List[Candle] = []
    for c in candles:
        if c.close_time <= close_time:
            out.append(c)
        else:
            break
    return out


def split_trades_by_time(trades: Sequence[BacktestTrade], train_frac: float) -> Tuple[List[BacktestTrade], List[BacktestTrade]]:
    if not trades:
        return [], []
    ordered = sorted(trades, key=lambda t: t.entry_time)
    cut = int(len(ordered) * clamp(train_frac, 0.2, 0.9))
    return ordered[:cut], ordered[cut:]


def compute_metrics(trades: Sequence[BacktestTrade]) -> Dict[str, Any]:
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    n = len(rs)
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = sum(rs) / n if n else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    stdev = statistics.pstdev(rs) if len(rs) > 1 else 0.0
    sharpe_like = expectancy / stdev * math.sqrt(max(n, 1)) if stdev > 0 else 0.0
    sqn = expectancy / stdev * math.sqrt(n) if stdev > 0 and n > 0 else 0.0
    avg_bars = sum(t.bars for t in trades) / n if n else 0.0
    return {
        "trades": n,
        "win_rate": round(win_rate, 4),
        "avg_win_R": round(avg_win, 4),
        "avg_loss_R": round(avg_loss, 4),
        "expectancy_R": round(expectancy, 4),
        "profit_factor": round(profit_factor, 4),
        "total_R": round(sum(rs), 4),
        "max_drawdown_R": round(max_dd, 4),
        "sharpe_like": round(sharpe_like, 4),
        "sqn": round(sqn, 4),
        "avg_bars": round(avg_bars, 2),
        "by_symbol": by_group_metrics(trades, "symbol"),
        "by_setup": by_group_metrics(trades, "setup"),
        "by_direction": by_group_metrics(trades, "direction"),
    }


def by_group_metrics(trades: Sequence[BacktestTrade], attr: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[BacktestTrade]] = {}
    for t in trades:
        key = str(getattr(t, attr))
        groups.setdefault(key, []).append(t)
    out: Dict[str, Dict[str, Any]] = {}
    for k, rows in groups.items():
        rs = [t.r_multiple for t in rows]
        out[k] = {"trades": len(rows), "expectancy_R": round(sum(rs) / len(rs), 4), "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 4), "total_R": round(sum(rs), 4)}
    return out


def robust_param_score(train: Dict[str, Any], test: Dict[str, Any], min_trades: int) -> float:
    if test.get("trades", 0) < min_trades:
        return -999.0 + test.get("trades", 0)
    # 训练集不能太好而测试集崩；优先测试集正期望和风险控制。
    test_exp = safe_float(test.get("expectancy_R"), 0.0)
    train_exp = safe_float(train.get("expectancy_R"), 0.0)
    pf = min(safe_float(test.get("profit_factor"), 0.0), 3.0)
    dd = safe_float(test.get("max_drawdown_R"), 0.0)
    stability_penalty = abs(train_exp - test_exp) * 0.8
    return test_exp * 100 + pf * 8 - dd * 1.5 - stability_penalty * 100


def write_backtest_csv(path: Path, trades: Sequence[BacktestTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "direction", "setup", "entry_time", "exit_time", "entry", "exit", "stop_loss", "tp1", "tp2", "r_multiple", "pnl_pct", "bars", "score", "reason"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            row = dataclasses.asdict(t)
            row["entry_time"] = ms_to_iso(t.entry_time)
            row["exit_time"] = ms_to_iso(t.exit_time)
            w.writerow(row)


# =============================================================================
# 主应用
# =============================================================================


class BrainV4:
    def __init__(self, settings: Settings):
        self.settings = settings
        setup_logging(settings)
        self.client = BinanceFuturesClient(settings)
        self.journal = TradeJournal(settings.journal_file)
        self.rules = self.client.symbol_rules()
        self.scanner = MarketScanner(settings, self.client)
        self.strategy = StrategyEngine(settings, self.client)
        self.risk = RiskManager(settings, self.rules)
        self.backtester = Backtester(settings, self.client)
        self.broker: Any = PaperBroker(settings, self.journal) if settings.trading_mode == "paper" else ExchangeBroker(settings, self.client, self.journal, self.rules)

    def universe(self) -> List[Tuple[str, float]]:
        return self.scanner.tradable_symbols()[: self.settings.max_symbols_per_cycle]

    def scan(self) -> Tuple[MarketRegime, List[TradeSignal]]:
        regime = self.strategy.analyze_regime()
        universe = self.universe()
        logging.info("市场环境: %s score=%.1f BTC=%s ATR%%=%.2f | %s", regime.bias, regime.score, fmt_num(regime.btc_price), regime.atr_pct, " / ".join(regime.reason))
        signals: List[TradeSignal] = []
        for sym, qv in universe:
            sig = self.strategy.analyze_symbol(sym, qv, regime)
            signals.append(sig)
            if sig.allowed:
                logging.info("候选 %s %s score=%.1f setup=%s entry=%s sl=%s tp2=%s | %s", sig.symbol, sig.direction, sig.score, sig.setup, fmt_num(sig.entry), fmt_num(sig.stop_loss), fmt_num(sig.tp2), " / ".join(sig.reasons[:3]))
        signals.sort(key=lambda s: s.score, reverse=True)
        return regime, signals

    def mark_prices_for(self, signals: Sequence[TradeSignal]) -> Dict[str, float]:
        marks = {s.symbol: s.entry for s in signals if s.entry > 0}
        for sym, pos in self.broker.positions().items():
            if sym not in marks:
                try:
                    rows = self.client.klines(sym, self.settings.entry_interval, 2)
                    marks[sym] = rows[-1].close if rows else safe_float(pos.get("entry"), 0.0)
                except Exception:
                    marks[sym] = safe_float(pos.get("entry"), 0.0) if isinstance(pos, dict) else 0.0
        return marks

    def run_once(self, execute: bool = True) -> List[TradeSignal]:
        _regime, signals = self.scan()
        marks = self.mark_prices_for(signals)
        self.broker.manage_positions(marks)
        marks = self.mark_prices_for(signals)
        ok, reason = self.broker.can_open_today(marks)
        if not ok:
            logging.warning("停止开新仓：%s", reason)
            return signals
        if not execute:
            return signals
        open_positions = self.broker.positions()
        total_notional = self.broker.total_notional(marks)
        equity = self.broker.equity(marks)
        cooldowns = self.broker.cooldowns()
        opened = 0
        for sig in signals:
            if opened >= self.settings.max_new_entries_per_cycle:
                break
            plan, blockers = self.risk.build_plan(sig, equity, open_positions, total_notional, cooldowns)
            if not plan:
                continue
            try:
                self.broker.open_position(plan, sig)
                opened += 1
                open_positions = self.broker.positions()
                total_notional = self.broker.total_notional(marks)
            except Exception:
                logging.error("开仓失败 %s\n%s", sig.symbol, traceback.format_exc())
        if opened == 0:
            logging.info("本轮没有满足风控和评分的开仓机会。")
        return signals

    def run_backtest(self) -> Tuple[List[BacktestTrade], Dict[str, Any]]:
        trades = self.backtester.backtest_many(self.universe())
        metrics = compute_metrics(trades)
        write_backtest_csv(self.settings.backtest_file, trades)
        self.settings.metrics_file.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return trades, metrics

    def run_optimize(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        best, results = self.backtester.walk_forward_optimize(self.universe())
        out_path = self.settings.workspace / "optimization_results.json"
        out_path.write_text(json.dumps({"best": best, "results": results}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return best, results

    def print_positions(self) -> None:
        marks: Dict[str, float] = {}
        for sym, pos in self.broker.positions().items():
            try:
                rows = self.client.klines(sym, self.settings.entry_interval, 2)
                marks[sym] = rows[-1].close if rows else safe_float(pos.get("entry"), 0.0)
            except Exception:
                marks[sym] = safe_float(pos.get("entry"), 0.0) if isinstance(pos, dict) else 0.0
        positions = self.broker.positions()
        if not positions:
            print("无持仓")
            print(f"equity={fmt_num(self.broker.equity(marks))}")
            return
        for sym, pos in positions.items():
            print(json.dumps({sym: pos, "mark": marks.get(sym)}, ensure_ascii=False, indent=2))
        bal = getattr(self.broker, "balance", lambda: 0.0)()
        print(f"balance={fmt_num(bal)} equity={fmt_num(self.broker.equity(marks))}")


# =============================================================================
# V5：机构级增强层（数据质量、市场宽度、相对强弱、OI/资金费率、新闻风险、Edge Memory）
# =============================================================================

@dataclass
class DataQualityReport:
    ok: bool
    gaps: int
    duplicates: int
    zero_volume: int
    bad_ohlc: int
    missing_ratio: float
    reason: List[str] = field(default_factory=list)


def validate_candles(candles: Sequence[Candle], interval: str, min_len: int = 80) -> DataQualityReport:
    reasons: List[str] = []
    if len(candles) < min_len:
        return DataQualityReport(False, 0, 0, 0, 0, 1.0, [f"K线不足 {len(candles)} < {min_len}"])
    step = interval_to_ms(interval)
    gaps = duplicates = zero_volume = bad_ohlc = 0
    last_t: Optional[int] = None
    for c in candles:
        if last_t is not None:
            if c.open_time == last_t:
                duplicates += 1
            elif c.open_time - last_t > step * 1.5:
                gaps += 1
        last_t = c.open_time
        if c.volume <= 0:
            zero_volume += 1
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.high < c.low:
            bad_ohlc += 1
    missing_ratio = gaps / max(len(candles), 1)
    if gaps:
        reasons.append(f"K线缺口 {gaps}")
    if duplicates:
        reasons.append(f"重复K线 {duplicates}")
    if zero_volume > max(3, len(candles) * 0.05):
        reasons.append(f"零成交K线过多 {zero_volume}")
    if bad_ohlc:
        reasons.append(f"OHLC异常 {bad_ohlc}")
    ok = not reasons or (bad_ohlc == 0 and missing_ratio < 0.02 and duplicates == 0)
    return DataQualityReport(ok, gaps, duplicates, zero_volume, bad_ohlc, missing_ratio, reasons)


def simple_return(candles: Sequence[Candle], bars: int) -> float:
    if len(candles) <= bars or candles[-bars - 1].close <= 0:
        return 0.0
    return candles[-1].close / candles[-bars - 1].close - 1.0


def corr_returns(a: Sequence[Candle], b: Sequence[Candle], bars: int = 80) -> float:
    n = min(len(a), len(b), bars + 1)
    if n < 20:
        return 0.0
    ac, bc = closes(a[-n:]), closes(b[-n:])
    ar, br = [], []
    for i in range(1, n):
        if ac[i - 1] > 0 and bc[i - 1] > 0:
            ar.append(ac[i] / ac[i - 1] - 1.0)
            br.append(bc[i] / bc[i - 1] - 1.0)
    if len(ar) < 10 or len(ar) != len(br):
        return 0.0
    ma, mb = sum(ar) / len(ar), sum(br) / len(br)
    va, vb = sum((x - ma) ** 2 for x in ar), sum((y - mb) ** 2 for y in br)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(ar, br))
    return clamp(cov / math.sqrt(va * vb), -1.0, 1.0)


class EdgeMemory:
    """用历史回测/实盘交易结果给 symbol+direction+setup 做正期望过滤。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = self.load()
        self.min_trades = env_int("EDGE_MIN_TRADES", 12)
        self.min_expectancy = env_float("EDGE_MIN_EXPECTANCY_R", -0.03)
        self.strong_expectancy = env_float("EDGE_STRONG_EXPECTANCY_R", 0.12)
        self.enabled = env_bool("USE_EDGE_MEMORY", True)

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"updated_at": "", "keys": {}}
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            logging.exception("edge memory 读取失败")
        return {"updated_at": "", "keys": {}}

    def save(self) -> None:
        self.data["updated_at"] = utc_now().isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def key(symbol: str, direction: str, setup: str) -> str:
        return f"{symbol}|{direction}|{setup or 'unknown'}"

    def stats_for(self, symbol: str, direction: str, setup: str) -> Optional[Dict[str, Any]]:
        return self.data.get("keys", {}).get(self.key(symbol, direction, setup))

    def evaluate_signal(self, signal: TradeSignal) -> Tuple[float, List[str], List[str]]:
        if not self.enabled or signal.entry <= 0:
            return 0.0, [], []
        direction = signal.direction if signal.direction in {"LONG", "SHORT"} else ("LONG" if signal.tp2 > signal.entry else "SHORT")
        stats = self.stats_for(signal.symbol, direction, signal.setup)
        if not stats:
            return 0.0, ["EdgeMemory 无历史样本"], []
        n = int(stats.get("trades", 0))
        exp = safe_float(stats.get("expectancy_R"), 0.0)
        pf = safe_float(stats.get("profit_factor"), 0.0)
        if n >= self.min_trades and exp < self.min_expectancy:
            return -20.0, [], [f"EdgeMemory 历史期望偏弱 {exp:.3f}R/{n}笔"]
        bonus, reasons = 0.0, []
        if n >= self.min_trades and exp >= self.strong_expectancy:
            bonus += 8.0
            reasons.append(f"EdgeMemory 正期望 {exp:.3f}R PF={pf:.2f} n={n}")
        elif n >= self.min_trades and exp > 0:
            bonus += 4.0
            reasons.append(f"EdgeMemory 小幅正期望 {exp:.3f}R n={n}")
        return bonus, reasons, []

    def rebuild_from_trades(self, trades: Sequence[BacktestTrade]) -> Dict[str, Any]:
        grouped: Dict[str, List[BacktestTrade]] = {}
        for t in trades:
            grouped.setdefault(self.key(t.symbol, t.direction, t.setup), []).append(t)
        keys: Dict[str, Dict[str, Any]] = {}
        for k, rows in grouped.items():
            m = compute_metrics(rows)
            keys[k] = {x: m.get(x, 0) for x in ["trades", "win_rate", "expectancy_R", "profit_factor", "total_R", "max_drawdown_R"]}
        self.data = {"updated_at": utc_now().isoformat(), "keys": keys}
        self.save()
        return self.data


class NewsRiskProvider:
    """新闻/事件风险层：veto/加权，不直接驱动下单。"""

    def __init__(self, path_raw: str):
        self.path = Path(path_raw).expanduser() if path_raw else None
        self.enabled = bool(self.path) and env_bool("USE_NEWS_RISK", True)
        self.cache: List[Dict[str, Any]] = []
        self._mtime = 0.0

    def load(self) -> List[Dict[str, Any]]:
        if not self.enabled or not self.path or not self.path.exists():
            return []
        try:
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return self.cache
            self._mtime = mtime
            if self.path.suffix.lower() == ".json":
                obj = json.loads(self.path.read_text(encoding="utf-8"))
                rows = obj if isinstance(obj, list) else obj.get("events", []) if isinstance(obj, dict) else []
            else:
                with self.path.open("r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
            self.cache = [r for r in rows if isinstance(r, dict)]
        except Exception:
            logging.exception("新闻风险文件读取失败: %s", self.path)
            self.cache = []
        return self.cache

    def active_events(self, symbol: str) -> List[Dict[str, Any]]:
        rows, now = self.load(), utc_now()
        base, out = symbol.upper().replace("USDT", ""), []
        for r in rows:
            sym = str(r.get("symbol", r.get("asset", ""))).upper()
            if sym and sym not in {symbol.upper(), base}:
                continue
            exp = str(r.get("expires_at", r.get("expiry", ""))).strip()
            if exp:
                try:
                    d = dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=UTC)
                    if now > d:
                        continue
                except Exception:
                    pass
            out.append(r)
        return out

    def evaluate(self, symbol: str, direction: str) -> Tuple[float, List[str], List[str]]:
        bonus, reasons, blockers = 0.0, [], []
        for ev in self.active_events(symbol):
            impact = str(ev.get("impact", ev.get("type", ""))).lower()
            severity = int(safe_float(ev.get("severity", 1), 1))
            title = str(ev.get("title", ev.get("reason", "event")))[:80]
            if impact in {"negative", "bad", "risk", "delist", "hack", "exploit"}:
                if severity >= 2:
                    blockers.append(f"新闻/事件否决: {title}")
                else:
                    bonus -= 6
                    reasons.append(f"轻度负面事件: {title}")
            elif impact in {"positive", "good", "catalyst", "upgrade", "listing"}:
                bonus += min(5.0, 2.0 + severity)
                reasons.append(f"事件催化: {title}")
        return bonus, reasons, blockers


class InstitutionalDataMixin:
    def _safe_public(self, path: str, params: Optional[Dict[str, Any]] = None, default: Any = None) -> Any:
        try:
            return self.client.public("GET", path, params or {}) if getattr(self, "client", None) else default
        except Exception:
            return default

    def _book_ticker_map(self) -> Dict[str, Dict[str, Any]]:
        rows = self._safe_public("/fapi/v1/ticker/bookTicker", default=[])
        if isinstance(rows, dict):
            rows = [rows]
        return {str(r.get("symbol", "")).upper(): r for r in rows if isinstance(r, dict)}


class MarketScannerV5(MarketScanner, InstitutionalDataMixin):
    def tradable_symbols(self) -> List[Tuple[str, float]]:
        base_rows = super().tradable_symbols()
        if not env_bool("USE_SPREAD_FILTER", True):
            return base_rows
        max_spread_bps = env_float("MAX_SPREAD_BPS", 8.0)
        min_top_book_usdt = env_float("MIN_TOP_BOOK_USDT", 0.0)
        bt = self._book_ticker_map()
        filtered: List[Tuple[str, float]] = []
        for sym, qv in base_rows:
            row = bt.get(sym)
            if not row:
                filtered.append((sym, qv))
                continue
            bid, ask = safe_float(row.get("bidPrice"), 0.0), safe_float(row.get("askPrice"), 0.0)
            bid_qty, ask_qty = safe_float(row.get("bidQty"), 0.0), safe_float(row.get("askQty"), 0.0)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
            spread_bps = (ask - bid) / mid * 10000 if mid > 0 else 9999.0
            top_book = min(bid * bid_qty, ask * ask_qty) if bid > 0 and ask > 0 else 0.0
            if spread_bps > max_spread_bps:
                continue
            if min_top_book_usdt > 0 and top_book < min_top_book_usdt:
                continue
            filtered.append((sym, qv))
        return filtered


class StrategyEngineV5(StrategyEngine, InstitutionalDataMixin):
    def __init__(self, settings: Settings, client: Optional[BinanceFuturesClient] = None):
        super().__init__(settings, client)
        self._btc_cache: Dict[str, Tuple[float, List[Candle]]] = {}
        self.edge_memory = EdgeMemory(settings.workspace / "edge_memory.json")
        self.news = NewsRiskProvider(env_str("NEWS_RISK_FILE", str(settings.workspace / "news_events.json")))

    def btc_candles(self, interval: str, limit: int) -> List[Candle]:
        key = f"{interval}:{limit}"
        ts, rows = self._btc_cache.get(key, (0.0, []))
        if rows and time.time() - ts < 60:
            return rows
        if not self.client:
            return []
        rows = self.client.klines("BTCUSDT", interval, limit)
        self._btc_cache[key] = (time.time(), rows)
        return rows

    def market_breadth(self) -> Dict[str, Any]:
        if not self.client:
            return {"up_ratio": 0.5, "count": 0, "reason": "无 client"}
        try:
            rules, tickers, valid = self.client.symbol_rules(), self.client.tickers_24h(), []
            for t in tickers:
                sym = str(t.get("symbol", "")).upper()
                rule = rules.get(sym)
                qv = safe_float(t.get("quoteVolume"), 0.0)
                if rule and rule.status == "TRADING" and rule.contract_type == "PERPETUAL" and rule.quote_asset == "USDT" and qv >= self.settings.min_quote_volume:
                    valid.append(safe_float(t.get("priceChangePercent"), 0.0))
            if not valid:
                return {"up_ratio": 0.5, "count": 0, "reason": "无有效 breadth"}
            up_ratio, median_chg = sum(1 for x in valid if x > 0) / len(valid), percentile(valid, 0.5)
            return {"up_ratio": up_ratio, "count": len(valid), "median_change_pct": median_chg, "reason": f"全市场上涨占比 {up_ratio:.0%}, 中位涨跌 {median_chg:.2f}%"}
        except Exception as exc:
            return {"up_ratio": 0.5, "count": 0, "reason": f"breadth error {exc}"}

    def analyze_regime(self) -> MarketRegime:
        regime = super().analyze_regime()
        if not env_bool("USE_MARKET_BREADTH", True):
            return regime
        br = self.market_breadth()
        up_ratio = safe_float(br.get("up_ratio"), 0.5)
        reasons = list(regime.reason) + [str(br.get("reason", ""))]
        score, bias = regime.score, regime.bias
        if up_ratio >= 0.66:
            score += 8
            bias = "LONG" if bias == "NEUTRAL" else bias
            reasons.append("市场宽度支持多头")
        elif up_ratio <= 0.34:
            score += 8
            bias = "SHORT" if bias == "NEUTRAL" else bias
            reasons.append("市场宽度支持空头")
        elif 0.44 <= up_ratio <= 0.56 and regime.bias == "NEUTRAL":
            reasons.append("市场宽度中性，降低交易冲动")
        return MarketRegime(bias, clamp(score, 0, 100), regime.btc_price, regime.atr_pct, reasons)

    def derivative_features(self, symbol: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"oi_change_pct": 0.0, "ls_ratio": 1.0, "taker_buy_sell_ratio": 1.0}
        rows = self._safe_public("/futures/data/openInterestHist", {"symbol": symbol, "period": "15m", "limit": 12}, default=[])
        try:
            if isinstance(rows, list) and len(rows) >= 3:
                first, last = safe_float(rows[0].get("sumOpenInterest"), 0.0), safe_float(rows[-1].get("sumOpenInterest"), 0.0)
                if first > 0:
                    out["oi_change_pct"] = (last / first - 1.0) * 100.0
        except Exception:
            pass
        rows = self._safe_public("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": "15m", "limit": 1}, default=[])
        try:
            if isinstance(rows, list) and rows:
                out["ls_ratio"] = safe_float(rows[-1].get("longShortRatio"), 1.0)
        except Exception:
            pass
        rows = self._safe_public("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "15m", "limit": 1}, default=[])
        try:
            if isinstance(rows, list) and rows:
                out["taker_buy_sell_ratio"] = safe_float(rows[-1].get("buySellRatio"), 1.0)
        except Exception:
            pass
        return out

    def adjust_signal_with_institutional_factors(self, sig: TradeSignal, entry: Sequence[Candle], trend: Sequence[Candle], regime: MarketRegime) -> TradeSignal:
        if sig.entry <= 0:
            return sig
        direction_hint = sig.direction if sig.direction in {"LONG", "SHORT"} else ("LONG" if sig.tp2 > sig.entry else "SHORT")
        score, reasons, blockers = sig.score, list(sig.reasons), list(sig.blockers)
        q_entry, q_trend = validate_candles(entry, self.settings.entry_interval, 80), validate_candles(trend, self.settings.trend_interval, 100)
        if not q_entry.ok:
            blockers.append("entry 数据质量差: " + "/".join(q_entry.reason[:2]))
        if not q_trend.ok:
            blockers.append("trend 数据质量差: " + "/".join(q_trend.reason[:2]))

        btc_trend = self.btc_candles(self.settings.trend_interval, min(self.settings.kline_limit, 240))
        if btc_trend:
            rs_12, rs_48, corr = simple_return(trend, 12) - simple_return(btc_trend, 12), simple_return(trend, 48) - simple_return(btc_trend, 48), corr_returns(trend, btc_trend, 80)
            if direction_hint == "LONG":
                if rs_12 > 0.006 and rs_48 > 0:
                    score += 10; reasons.append(f"相对 BTC 强势 rs12={rs_12*100:.2f}% rs48={rs_48*100:.2f}%")
                elif rs_12 < -0.006:
                    score -= 10; reasons.append(f"相对 BTC 偏弱 rs12={rs_12*100:.2f}%")
            else:
                if rs_12 < -0.006 and rs_48 < 0:
                    score += 10; reasons.append(f"相对 BTC 弱势 rs12={rs_12*100:.2f}% rs48={rs_48*100:.2f}%")
                elif rs_12 > 0.006:
                    score -= 10; reasons.append(f"相对 BTC 偏强，不适合空 rs12={rs_12*100:.2f}%")
            if corr < 0.15 and regime.bias in {"LONG", "SHORT"}:
                score -= 3; reasons.append(f"与 BTC 相关性较低 corr={corr:.2f}")

        if env_bool("USE_DERIVATIVE_FILTERS", True):
            d = self.derivative_features(sig.symbol)
            oi, ls, taker = safe_float(d.get("oi_change_pct"), 0.0), safe_float(d.get("ls_ratio"), 1.0), safe_float(d.get("taker_buy_sell_ratio"), 1.0)
            if direction_hint == "LONG":
                if 0.5 <= oi <= 12 and taker >= 1.02:
                    score += 6; reasons.append(f"OI/主动买入确认 oi={oi:.1f}% taker={taker:.2f}")
                if ls > env_float("MAX_LONG_CROWD_RATIO", 2.6):
                    score -= 12; reasons.append(f"多头拥挤 ls={ls:.2f}")
            else:
                if 0.5 <= oi <= 12 and taker <= 0.98:
                    score += 6; reasons.append(f"OI/主动卖出确认 oi={oi:.1f}% taker={taker:.2f}")
                if ls < env_float("MIN_SHORT_CROWD_RATIO", 0.45):
                    score -= 12; reasons.append(f"空头拥挤 ls={ls:.2f}")
            if oi > env_float("MAX_OI_CHANGE_PCT", 25.0):
                blockers.append(f"OI 短时暴增，清算风险高 oi={oi:.1f}%")

        n_bonus, n_reasons, n_blockers = self.news.evaluate(sig.symbol, direction_hint)
        score += n_bonus; reasons.extend(n_reasons); blockers.extend(n_blockers)
        e_bonus, e_reasons, e_blockers = self.edge_memory.evaluate_signal(sig)
        score += e_bonus; reasons.extend(e_reasons); blockers.extend(e_blockers)

        score = clamp(score, 0, 100)
        blockers = [b for b in blockers if not str(b).startswith("评分不足")]
        if score < self.settings.score_threshold:
            blockers.append(f"评分不足 {score:.1f} < {self.settings.score_threshold:.1f}")
        sig.direction = direction_hint if not blockers else "NO_TRADE"
        sig.score, sig.reasons, sig.blockers = score, reasons[:12], blockers[:12]
        return sig

    def analyze_symbol(self, symbol: str, quote_volume: float, regime: MarketRegime) -> TradeSignal:
        if not self.client:
            return self._no_trade(symbol, quote_volume, ["无 client"])
        try:
            trend = self.client.klines(symbol, self.settings.trend_interval, self.settings.kline_limit)
            entry = self.client.klines(symbol, self.settings.entry_interval, self.settings.kline_limit)
            fund = safe_float(self.client.premium_index(symbol).get("lastFundingRate"), 0.0)
            sig = self.analyze_symbol_from_candles(symbol, entry, trend, regime, quote_volume, fund)
            return self.adjust_signal_with_institutional_factors(sig, entry, trend, regime)
        except Exception as exc:
            logging.debug("%s V5 分析失败: %s", symbol, exc)
            return self._no_trade(symbol, quote_volume, [f"分析异常: {exc}"])


class BrainV5(BrainV4):
    def __init__(self, settings: Settings):
        self.settings = settings
        setup_logging(settings)
        self.client = BinanceFuturesClient(settings)
        self.journal = TradeJournal(settings.journal_file)
        self.rules = self.client.symbol_rules()
        self.scanner = MarketScannerV5(settings, self.client)
        self.strategy = StrategyEngineV5(settings, self.client)
        self.risk = RiskManager(settings, self.rules)
        self.backtester = Backtester(settings, self.client)
        self.broker: Any = PaperBroker(settings, self.journal) if settings.trading_mode == "paper" else ExchangeBroker(settings, self.client, self.journal, self.rules)
        self.candidates_file = settings.workspace / "candidates.json"
        self.diagnostics_file = settings.workspace / "diagnostics.json"

    def scan(self) -> Tuple[MarketRegime, List[TradeSignal]]:
        regime, signals = super().scan()
        self.write_candidates(regime, signals)
        return regime, signals

    def write_candidates(self, regime: MarketRegime, signals: Sequence[TradeSignal]) -> None:
        rows = []
        for s in signals:
            rows.append({"symbol": s.symbol, "direction": s.direction, "score": round(s.score, 2), "setup": s.setup, "entry": s.entry, "stop_loss": s.stop_loss, "tp1": s.tp1, "tp2": s.tp2, "rr": s.rr, "atr_pct": s.atr_pct, "rsi": s.rsi, "funding_rate": s.funding_rate, "quote_volume": s.notional_volume, "reasons": s.reasons, "blockers": s.blockers})
        obj = {"time": utc_now().isoformat(), "regime": dataclasses.asdict(regime), "signals": rows}
        tmp = self.candidates_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.candidates_file)

    def run_backtest(self) -> Tuple[List[BacktestTrade], Dict[str, Any]]:
        trades, metrics = super().run_backtest()
        edge = self.strategy.edge_memory.rebuild_from_trades(trades)
        diagnostics = {"time": utc_now().isoformat(), "metrics": metrics, "edge_keys": len(edge.get("keys", {})), "institutional_notes": ["expectancy_R > 0 才说明平均每笔有正期望", "by_symbol/by_setup 用于构建白名单和黑名单", "样本少的币不要给高权重，避免过拟合"]}
        self.diagnostics_file.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return trades, metrics

    def edge_report(self, limit: int = 30) -> List[Tuple[str, Dict[str, Any]]]:
        data = self.strategy.edge_memory.load().get("keys", {})
        rows = list(data.items())
        rows.sort(key=lambda kv: (safe_float(kv[1].get("expectancy_R"), 0.0), safe_float(kv[1].get("profit_factor"), 0.0), int(kv[1].get("trades", 0))), reverse=True)
        return rows[:limit]


def print_edge_report(rows: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
    print("\nEdge Memory top setups")
    print("key                                      trades  expR     PF      win%    totalR   maxDD")
    print("-" * 100)
    for key, st in rows:
        print(f"{key:<40} {int(st.get('trades', 0)):>6} {safe_float(st.get('expectancy_R'), 0):>7.3f} {safe_float(st.get('profit_factor'), 0):>7.2f} {safe_float(st.get('win_rate'), 0)*100:>6.1f}% {safe_float(st.get('total_R'), 0):>7.2f} {safe_float(st.get('max_drawdown_R'), 0):>7.2f}")


# =============================================================================
# 输出
# =============================================================================


def print_scan_table(signals: Sequence[TradeSignal], limit: int = 15) -> None:
    rows = signals[:limit]
    print("\nTOP signals")
    print("symbol      dir       score setup      entry        sl           tp2          atr%   rsi   blockers/reasons")
    print("-" * 132)
    for s in rows:
        desc = " | ".join((s.reasons if s.allowed else s.blockers)[:2])
        print(f"{s.symbol:<11} {s.direction:<9} {s.score:>5.1f} {s.setup:<10} {fmt_num(s.entry):<12} {fmt_num(s.stop_loss):<12} {fmt_num(s.tp2):<12} {s.atr_pct:>5.2f} {s.rsi:>5.1f} {desc}")


def print_metrics(metrics: Dict[str, Any]) -> None:
    keys = ["trades", "win_rate", "expectancy_R", "profit_factor", "total_R", "max_drawdown_R", "sharpe_like", "sqn", "avg_bars"]
    print("\nBacktest metrics")
    print("-" * 64)
    for k in keys:
        print(f"{k:<20} {metrics.get(k)}")
    print("\nBy direction:")
    print(json.dumps(metrics.get("by_direction", {}), ensure_ascii=False, indent=2))
    print("\nBy setup:")
    print(json.dumps(metrics.get("by_setup", {}), ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Brain V11 / Institutional AI Quant Final OS")
    parser.add_argument("--scan", action="store_true", help="只扫描，不开仓")
    parser.add_argument("--once", action="store_true", help="运行一轮：管理持仓 + 扫描 + 可选开仓")
    parser.add_argument("--loop", action="store_true", help="循环运行")
    parser.add_argument("--positions", action="store_true", help="查看持仓")
    parser.add_argument("--backtest", action="store_true", help="对当前 universe 回测，并输出 metrics/trades")
    parser.add_argument("--optimize", action="store_true", help="Walk-forward 参数验证，输出 optimization_results.json")
    parser.add_argument("--show-config", action="store_true", help="打印配置")
    parser.add_argument("--edge-report", action="store_true", help="查看 Edge Memory 中历史正期望 setup")
    parser.add_argument("--limit", type=int, default=15, help="扫描表输出数量")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if args.model_card or args.final_audit or args.ai_files:
        os.environ.setdefault("V11_SKIP_SYMBOL_RULES_ON_START", "1")
    if args.show_config:
        print(json.dumps(settings.sanitized(), ensure_ascii=False, indent=2))
        return 0
    if args.download_history:
        syms = settings.symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        dest = Path(args.history_dir).expanduser().resolve()
        manifest = download_binance_vision_monthly(syms, args.history_interval, args.history_start, args.history_end, dest)
        print_json(manifest)
        print(f"\n下载完成。回测时设置：export LOCAL_KLINE_DIR={dest}")
        return 0

    if args.edge_report:
        data = EdgeMemory(settings.workspace / "edge_memory.json").load().get("keys", {})
        rows = list(data.items())
        rows.sort(key=lambda kv: (safe_float(kv[1].get("expectancy_R"), 0.0), safe_float(kv[1].get("profit_factor"), 0.0), int(kv[1].get("trades", 0))), reverse=True)
        print_edge_report(rows[: args.limit])
        return 0

    app = BrainV5(settings)
    logging.info("Brain V7 started mode=%s workspace=%s", settings.trading_mode, settings.workspace)

    if args.positions:
        app.print_positions()
        return 0
    if args.scan:
        _regime, signals = app.scan()
        print_scan_table(signals, args.limit)
        return 0
    if args.backtest:
        trades, metrics = app.run_backtest()
        print_metrics(metrics)
        print(f"\ntrades csv: {settings.backtest_file}")
        print(f"metrics json: {settings.metrics_file}")
        return 0
    if args.optimize:
        best, _results = app.run_optimize()
        print("\nBest walk-forward params")
        print(json.dumps(best, ensure_ascii=False, indent=2))
        print(f"\noptimization json: {settings.workspace / 'optimization_results.json'}")
        return 0
    if args.loop:
        while True:
            try:
                signals = app.run_once(execute=True)
                print_scan_table(signals, args.limit)
            except KeyboardInterrupt:
                logging.info("收到中断，退出。")
                return 0
            except Exception:
                logging.error("主循环异常\n%s", traceback.format_exc())
            time.sleep(settings.loop_seconds)

    signals = app.run_once(execute=True)
    print_scan_table(signals, args.limit)
    return 0




# =============================================================================
# V6：AI Agent / Ai Pro 控制层
# =============================================================================

@dataclass
class AIDecision:
    action: str = "NO_TRADE"          # TRADE / NO_TRADE
    symbol: str = ""
    direction: str = ""              # LONG / SHORT
    confidence: float = 0.0
    reason: List[str] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


class AiDecisionLayer:
    """AI 决策层。

    支持三种模式：
      - off：不使用 AI，保持纯量化排序；
      - rule：本地可复现的“AI 模拟决策”，从风控已通过候选中选择最佳；
      - external/aipro：读取 AI/Ai Pro 产出的 JSON 文件，只允许从候选列表里选，不允许修改入场/止损/仓位。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = env_str("AI_DECISION_MODE", "rule").lower()
        self.external_file = Path(env_str("AI_DECISION_FILE", str(settings.workspace / "ai_decision_input.json"))).expanduser()
        self.min_confidence = env_float("AI_MIN_CONFIDENCE", 0.62)
        self.fallback_to_rule = env_bool("AI_FALLBACK_TO_RULE", True)

    def decide(self, payload: Dict[str, Any], max_decisions: int) -> List[AIDecision]:
        if self.mode in {"off", "none", "0", "false"}:
            return []
        if self.mode in {"external", "aipro", "ai_pro"}:
            decisions = self._read_external_decisions()
            if decisions:
                return decisions[:max_decisions]
            if not self.fallback_to_rule:
                return [AIDecision(action="NO_TRADE", reason=[f"未找到外部 AI 决策文件: {self.external_file}"])]
        return self._rule_decisions(payload, max_decisions)

    def _rule_decisions(self, payload: Dict[str, Any], max_decisions: int) -> List[AIDecision]:
        rows = [r for r in payload.get("candidates", []) if r.get("risk_approved") and r.get("direction") in {"LONG", "SHORT"}]
        rows.sort(key=lambda r: (safe_float(r.get("ai_priority"), 0.0), safe_float(r.get("score"), 0.0), safe_float(r.get("rr"), 0.0)), reverse=True)
        out: List[AIDecision] = []
        for r in rows[:max_decisions]:
            score = safe_float(r.get("score"), 0.0)
            conf = clamp(0.45 + score / 200.0 + safe_float(r.get("edge_expectancy_R"), 0.0) * 0.25, 0.0, 0.95)
            out.append(AIDecision(
                action="TRADE",
                symbol=str(r.get("symbol", "")),
                direction=str(r.get("direction", "")),
                confidence=conf,
                reason=["rule-ai: 选择风控已通过且综合优先级最高的候选", *list(r.get("reasons", []))[:4]],
                risk_notes=list(r.get("risk_notes", [])),
                raw={"source": "rule", "candidate_id": r.get("candidate_id")},
            ))
        return out or [AIDecision(action="NO_TRADE", reason=["无风控通过候选"])]

    def _read_external_decisions(self) -> List[AIDecision]:
        if not self.external_file.exists():
            return []
        try:
            obj = json.loads(self.external_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.error("AI 决策文件读取失败: %s", exc)
            return [AIDecision(action="NO_TRADE", reason=[f"AI 决策文件无法解析: {exc}"])]
        rows: List[Dict[str, Any]]
        if isinstance(obj, dict) and isinstance(obj.get("decisions"), list):
            rows = [x for x in obj.get("decisions", []) if isinstance(x, dict)]
        elif isinstance(obj, list):
            rows = [x for x in obj if isinstance(x, dict)]
        elif isinstance(obj, dict):
            rows = [obj]
        else:
            rows = []
        out: List[AIDecision] = []
        for r in rows:
            action_raw = str(r.get("action", r.get("decision", "NO_TRADE"))).upper()
            direction_raw = str(r.get("direction", r.get("side", ""))).upper()
            if action_raw in {"LONG", "SHORT"} and not direction_raw:
                direction_raw = action_raw
                action_raw = "TRADE"
            if action_raw in {"BUY", "SELL"}:
                direction_raw = "LONG" if action_raw == "BUY" else "SHORT"
                action_raw = "TRADE"
            if action_raw not in {"TRADE", "NO_TRADE"}:
                action_raw = "NO_TRADE"
            out.append(AIDecision(
                action=action_raw,
                symbol=str(r.get("symbol", "")).upper(),
                direction=direction_raw,
                confidence=safe_float(r.get("confidence", r.get("score", 0.0)), 0.0),
                reason=list(r.get("reason", r.get("reasons", []))) if isinstance(r.get("reason", r.get("reasons", [])), list) else [str(r.get("reason", r.get("reasons", "")))],
                risk_notes=list(r.get("risk_notes", [])) if isinstance(r.get("risk_notes", []), list) else [str(r.get("risk_notes", ""))],
                raw=r,
            ))
        return out


class RiskGovernor:
    """AI 最终风控闸门。AI 只能选择候选，不能修改 plan；这里拥有最终否决权。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.min_confidence = env_float("AI_MIN_CONFIDENCE", 0.62)
        self.live_max_order_notional = env_float("LIVE_MAX_ORDER_NOTIONAL", 0.0)
        self.live_allowed_symbols = set(parse_csv_symbols(env_str("LIVE_ALLOWED_SYMBOLS", "")))
        self.live_force_edge = env_bool("LIVE_FORCE_EDGE_MEMORY", False)
        self.live_min_edge_trades = env_int("LIVE_MIN_EDGE_TRADES", 20)
        self.live_min_expectancy = env_float("LIVE_MIN_EXPECTANCY_R", 0.02)

    def approve(self, decision: AIDecision, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        blockers: List[str] = []
        if decision.action != "TRADE":
            return None, decision.reason or ["AI 决策 NO_TRADE"]
        if decision.confidence < self.min_confidence:
            blockers.append(f"AI 置信度不足 {decision.confidence:.2f} < {self.min_confidence:.2f}")
        if decision.direction not in {"LONG", "SHORT"}:
            blockers.append("AI direction 非 LONG/SHORT")
        if not decision.symbol:
            blockers.append("AI 未指定 symbol")

        candidates = payload.get("candidates", [])
        match = None
        for row in candidates:
            if row.get("symbol") == decision.symbol and row.get("direction") == decision.direction:
                match = row
                break
        if not match:
            blockers.append("AI 选择不在候选列表内，拒绝")
            return None, blockers
        if not match.get("risk_approved"):
            blockers.append("候选未通过量化风控")
        if not match.get("plan"):
            blockers.append("候选缺少下单 plan")
        if safe_float(match.get("score"), 0.0) < self.settings.score_threshold:
            blockers.append("量化评分低于阈值")
        if safe_float(match.get("rr"), 0.0) < self.settings.min_rr:
            blockers.append("RR 低于阈值")

        if self.settings.trading_mode == "live":
            if self.live_allowed_symbols and decision.symbol not in self.live_allowed_symbols:
                blockers.append(f"live 白名单未允许 {decision.symbol}")
            if self.live_max_order_notional > 0 and safe_float(match.get("plan", {}).get("notional"), 0.0) > self.live_max_order_notional:
                blockers.append("live 单笔名义价值超过 LIVE_MAX_ORDER_NOTIONAL")
            if self.live_force_edge:
                n = int(match.get("edge_trades", 0))
                exp = safe_float(match.get("edge_expectancy_R"), 0.0)
                if n < self.live_min_edge_trades or exp < self.live_min_expectancy:
                    blockers.append(f"live EdgeMemory 不足：n={n} exp={exp:.3f}R")

        if blockers:
            return None, blockers
        return match, ["APPROVED"]


def _plan_from_dict(row: Dict[str, Any]) -> OrderPlan:
    p = row["plan"]
    return OrderPlan(
        symbol=str(p["symbol"]), direction=str(p["direction"]), entry=safe_float(p["entry"]),
        stop_loss=safe_float(p["stop_loss"]), tp1=safe_float(p["tp1"]), tp2=safe_float(p["tp2"]),
        qty=safe_float(p["qty"]), notional=safe_float(p["notional"]), risk_usdt=safe_float(p["risk_usdt"]),
        score=safe_float(p["score"]), setup=str(p.get("setup", "")), reasons=list(p.get("reasons", [])),
    )


class ExchangeBrokerV6(ExchangeBroker):
    """demo/live 交易所执行层，增加本地日内熔断状态。

    注意：真实止盈止损由交易所 algo protection 负责；本地只做开仓前风控和审计。
    """

    def __init__(self, settings: Settings, client: BinanceFuturesClient, journal: TradeJournal, rules: Dict[str, SymbolRules]):
        super().__init__(settings, client, journal, rules)
        self.store = JsonStore(settings.workspace / f"{settings.trading_mode}_risk_state.json", {"day": utc_day(), "day_start_equity": 0.0})
        self.state = self.store.load()
        self._roll_day_if_needed()

    def _roll_day_if_needed(self) -> None:
        day = utc_day()
        if self.state.get("day") != day or safe_float(self.state.get("day_start_equity"), 0.0) <= 0:
            self.state["day"] = day
            self.state["day_start_equity"] = max(self.equity({}), 0.0)
            self.store.save(self.state)

    def can_open_today(self, mark_prices: Dict[str, float]) -> Tuple[bool, str]:
        self._roll_day_if_needed()
        start = safe_float(self.state.get("day_start_equity"), 0.0)
        eq = self.equity(mark_prices)
        if start <= 0 or eq <= 0:
            return False, "交易所权益无效，拒绝开新仓"
        loss = max(0.0, (start - eq) / start)
        if loss >= self.settings.daily_max_loss_pct:
            return False, f"交易所日内亏损熔断 {loss:.2%} >= {self.settings.daily_max_loss_pct:.2%}"
        return True, "OK"


class BrainV6(BrainV5):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        # 用 V6 交易所执行层替换 V5 执行层，paper 保持本地模拟。
        self.broker = PaperBroker(settings, self.journal) if settings.trading_mode == "paper" else ExchangeBrokerV6(settings, self.client, self.journal, self.rules)
        self.ai_layer = AiDecisionLayer(settings)
        self.governor = RiskGovernor(settings)
        self.ai_candidates_file = settings.workspace / "ai_candidates.json"
        self.ai_prompt_file = settings.workspace / "aipro_prompt.md"
        self.ai_decisions_log = settings.workspace / "ai_decisions.jsonl"

    def build_ai_payload(self, regime: MarketRegime, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> Dict[str, Any]:
        open_positions = self.broker.positions()
        total_notional = self.broker.total_notional(marks)
        equity = self.broker.equity(marks)
        cooldowns = self.broker.cooldowns()
        candidates: List[Dict[str, Any]] = []
        top_k = env_int("AI_TOP_K", 20)
        for sig in list(signals)[: max(top_k, 1)]:
            plan, risk_blockers = self.risk.build_plan(sig, equity, open_positions, total_notional, cooldowns)
            edge_stats: Dict[str, Any] = {}
            try:
                direction_for_edge = sig.direction if sig.direction in {"LONG", "SHORT"} else ("LONG" if sig.tp2 > sig.entry else "SHORT")
                edge_stats = self.strategy.edge_memory.stats_for(sig.symbol, direction_for_edge, sig.setup) or {}
            except Exception:
                edge_stats = {}
            edge_exp = safe_float(edge_stats.get("expectancy_R"), 0.0)
            edge_trades = int(edge_stats.get("trades", 0) or 0)
            risk_approved = plan is not None
            # 优先级：量化评分 + edge + RR - 拥挤/拒绝惩罚。只用于排序，不改变风控。
            ai_priority = safe_float(sig.score) + clamp(edge_exp * 25.0, -10.0, 10.0) + clamp(sig.rr - self.settings.min_rr, 0, 1.5) * 3.0
            if not risk_approved:
                ai_priority -= 40.0
            row = {
                "candidate_id": f"{sig.symbol}:{sig.direction}:{sig.setup}:{int(sig.score*10)}",
                "symbol": sig.symbol,
                "direction": sig.direction,
                "score": round(sig.score, 3),
                "setup": sig.setup,
                "entry": sig.entry,
                "stop_loss": sig.stop_loss,
                "tp1": sig.tp1,
                "tp2": sig.tp2,
                "rr": sig.rr,
                "atr": sig.atr,
                "atr_pct": sig.atr_pct,
                "rsi": sig.rsi,
                "funding_rate": sig.funding_rate,
                "quote_volume": sig.notional_volume,
                "edge_trades": edge_trades,
                "edge_expectancy_R": edge_exp,
                "edge_profit_factor": safe_float(edge_stats.get("profit_factor"), 0.0),
                "risk_approved": risk_approved,
                "risk_blockers": list(risk_blockers),
                "blockers": sig.blockers,
                "reasons": sig.reasons,
                "risk_notes": [f"单笔风险约 {fmt_num(plan.risk_usdt)} USDT" if plan else "未生成下单计划"],
                "ai_priority": round(ai_priority, 4),
                "plan": dataclasses.asdict(plan) if plan else None,
            }
            candidates.append(row)
        candidates.sort(key=lambda r: (bool(r.get("risk_approved")), safe_float(r.get("ai_priority"), 0.0)), reverse=True)
        payload = {
            "schema": "brain_v7_ai_candidates_v1",
            "time": utc_now().isoformat(),
            "mode": self.settings.trading_mode,
            "workspace": str(self.settings.workspace),
            "regime": dataclasses.asdict(regime),
            "account": {
                "equity": equity,
                "open_positions": len(open_positions),
                "total_notional": total_notional,
                "risk_per_trade": self.settings.risk_per_trade,
                "max_positions": self.settings.max_positions,
                "max_new_entries_per_cycle": self.settings.max_new_entries_per_cycle,
            },
            "policy": {
                "ai_can_only_select_existing_candidate": True,
                "ai_cannot_change_entry_sl_tp_qty": True,
                "min_score": self.settings.score_threshold,
                "min_rr": self.settings.min_rr,
                "min_confidence": self.ai_layer.min_confidence,
                "live_requires_confirm": self.settings.trading_mode == "live",
                "use_exchange_protection": self.settings.use_exchange_protection,
            },
            "candidates": candidates,
        }
        _atomic_write_json(self.ai_candidates_file, payload)
        self._write_aipro_prompt(payload)
        return payload

    def _write_aipro_prompt(self, payload: Dict[str, Any]) -> None:
        top = payload.get("candidates", [])[: env_int("AI_PROMPT_TOP_N", 8)]
        slim = {"schema": payload.get("schema"), "time": payload.get("time"), "mode": payload.get("mode"), "regime": payload.get("regime"), "account": payload.get("account"), "policy": payload.get("policy"), "candidates": top}
        prompt = f"""# Brain V7 / Ai Pro 决策任务

你是合约量化交易风控代理。你只能从下面 candidates 中选择，不允许创造 symbol，不允许修改 entry/stop_loss/tp/qty，不允许扩大仓位。

硬规则：
1. 如果没有 risk_approved=true 的候选，输出 NO_TRADE。
2. 只能选择 score >= min_score 且 rr >= min_rr 的候选。
3. 如果存在严重新闻/风控 blocker，输出 NO_TRADE。
4. 你只能输出 JSON，不能输出解释性散文。
5. 如果不确定，输出 NO_TRADE。

输出格式必须是：
```json
{{
  "decision": "TRADE" 或 "NO_TRADE",
  "symbol": "例如 SOLUSDT，NO_TRADE 时留空",
  "direction": "LONG 或 SHORT，NO_TRADE 时留空",
  "confidence": 0.0到1.0,
  "reason": ["选择或拒绝原因"],
  "risk_notes": ["你观察到的风险"]
}}
```

候选数据：
```json
{json.dumps(slim, ensure_ascii=False, indent=2)}
```
"""
        self.ai_prompt_file.write_text(prompt, encoding="utf-8")

    def run_once(self, execute: bool = True) -> List[TradeSignal]:
        regime, signals = self.scan()
        marks = self.mark_prices_for(signals)
        self.broker.manage_positions(marks)
        marks = self.mark_prices_for(signals)
        payload = self.build_ai_payload(regime, signals, marks)

        ok, reason = self.broker.can_open_today(marks)
        if not ok:
            logging.warning("停止开新仓：%s", reason)
            self._audit_decision(AIDecision(action="NO_TRADE", reason=[reason]), None, [reason], executed=False)
            return signals
        if not execute:
            return signals

        if self.ai_layer.mode in {"off", "none", "0", "false"}:
            return self._run_quant_execution(signals, marks)

        decisions = self.ai_layer.decide(payload, self.settings.max_new_entries_per_cycle)
        opened = 0
        for dec in decisions:
            if opened >= self.settings.max_new_entries_per_cycle:
                break
            match, gov_notes = self.governor.approve(dec, payload)
            if not match:
                logging.info("AI 决策未执行: %s | %s", dataclasses.asdict(dec), " / ".join(gov_notes))
                self._audit_decision(dec, None, gov_notes, executed=False)
                continue
            try:
                plan = _plan_from_dict(match)
                sig = next((s for s in signals if s.symbol == plan.symbol and s.direction == plan.direction), None)
                if sig is None:
                    self._audit_decision(dec, match, ["内部错误：找不到 signal"], executed=False)
                    continue
                self.broker.open_position(plan, sig)
                opened += 1
                self._audit_decision(dec, match, gov_notes, executed=True)
            except Exception:
                err = traceback.format_exc()
                logging.error("AI 开仓执行失败\n%s", err)
                self._audit_decision(dec, match, [err], executed=False)
        if opened == 0:
            logging.info("本轮 AI/风控没有执行开仓。")
        return signals

    def _run_quant_execution(self, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> List[TradeSignal]:
        open_positions = self.broker.positions()
        total_notional = self.broker.total_notional(marks)
        equity = self.broker.equity(marks)
        cooldowns = self.broker.cooldowns()
        opened = 0
        for sig in signals:
            if opened >= self.settings.max_new_entries_per_cycle:
                break
            plan, blockers = self.risk.build_plan(sig, equity, open_positions, total_notional, cooldowns)
            if not plan:
                continue
            try:
                self.broker.open_position(plan, sig)
                opened += 1
                open_positions = self.broker.positions()
                total_notional = self.broker.total_notional(marks)
            except Exception:
                logging.error("开仓失败 %s\n%s", sig.symbol, traceback.format_exc())
        if opened == 0:
            logging.info("纯量化模式：本轮没有满足风控和评分的开仓机会。")
        return list(signals)

    def _audit_decision(self, decision: AIDecision, candidate: Optional[Dict[str, Any]], notes: List[str], executed: bool) -> None:
        obj = {
            "time": utc_now().isoformat(),
            "mode": self.settings.trading_mode,
            "ai_mode": self.ai_layer.mode,
            "decision": dataclasses.asdict(decision),
            "candidate": candidate,
            "governor_notes": notes,
            "executed": executed,
        }
        _append_jsonl(self.ai_decisions_log, obj)




# =============================================================================
# V7：机构级流程优化层（波动状态、假突破过滤、盘口深度、组合相关性、日报）
# =============================================================================

def true_ranges(candles: Sequence[Candle]) -> List[float]:
    if len(candles) < 2:
        return []
    out: List[float] = []
    prev_close = candles[0].close
    for c in candles[1:]:
        out.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return out


def percentile_rank(values: Sequence[float], value: float) -> float:
    vals = [v for v in values if math.isfinite(v)]
    if not vals:
        return 0.5
    return sum(1 for v in vals if v <= value) / len(vals)


def choppiness_index(candles: Sequence[Candle], period: int = 14) -> float:
    """Choppiness 越高越震荡；越低越趋势。"""
    if len(candles) < period + 1:
        return 50.0
    window = list(candles[-period:])
    trs = true_ranges(candles[-period - 1:])[-period:]
    hh = max(c.high for c in window)
    ll = min(c.low for c in window)
    denom = max(hh - ll, 1e-12)
    num = max(sum(trs), 1e-12)
    try:
        return 100.0 * math.log10(num / denom) / math.log10(period)
    except Exception:
        return 50.0


def last_wick_profile(c: Candle) -> Dict[str, float]:
    rng = max(c.high - c.low, 1e-12)
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    body = abs(c.close - c.open)
    return {
        "range": rng,
        "upper_wick_ratio": upper / rng,
        "lower_wick_ratio": lower / rng,
        "body_ratio": body / rng,
        "close_location": (c.close - c.low) / rng,
    }


def realized_vol_pct(candles: Sequence[Candle], bars: int = 48) -> float:
    if len(candles) < bars + 1:
        return 0.0
    vals = closes(candles[-bars - 1:])
    rets = []
    for i in range(1, len(vals)):
        if vals[i-1] > 0:
            rets.append(math.log(vals[i] / vals[i-1]))
    if len(rets) < 5:
        return 0.0
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var) * math.sqrt(bars) * 100.0


class MarketScannerV7(MarketScannerV5):
    """V7 交易池：继续先按流动性/点差过滤，并为后续流程保留更严格默认值。"""

    def tradable_symbols(self) -> List[Tuple[str, float]]:
        rows = super().tradable_symbols()
        # 可选：进一步限制 24h 成交额分位，避免小币噪音过多。
        if not env_bool("USE_LIQUIDITY_TIER_FILTER", True) or len(rows) < 10:
            return rows
        min_rank = env_int("LIQUIDITY_MIN_RANK", 0)
        if min_rank > 0:
            rows = rows[:min(len(rows), min_rank)]
        return rows


class StrategyEngineV7(StrategyEngineV5):
    """V7 策略优化：在 V5 的基础上增加更严格的流程门。"""

    def __init__(self, settings: Settings, client: Optional[BinanceFuturesClient] = None):
        super().__init__(settings, client)
        self._depth_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._symbol_regime_cache: Dict[str, Tuple[float, List[Candle]]] = {}

    def symbol_regime_candles(self, symbol: str) -> List[Candle]:
        key = f"{symbol}:{self.settings.regime_interval}"
        ts, rows = self._symbol_regime_cache.get(key, (0.0, []))
        if rows and time.time() - ts < 90:
            return rows
        if not self.client:
            return []
        try:
            rows = self.client.klines(symbol, self.settings.regime_interval, min(self.settings.kline_limit, 240))
            self._symbol_regime_cache[key] = (time.time(), rows)
            return rows
        except Exception:
            return []

    def depth_features(self, symbol: str) -> Dict[str, Any]:
        ts, cached = self._depth_cache.get(symbol, (0.0, {}))
        if cached and time.time() - ts < 20:
            return cached
        out = {"spread_bps": 9999.0, "depth_usdt": 0.0, "imbalance": 0.0}
        if not self.client or not env_bool("USE_ORDERBOOK_DEPTH", True):
            return out
        try:
            limit = env_int("DEPTH_LIMIT", 20)
            data = self.client.public("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
            bids = data.get("bids", []) if isinstance(data, dict) else []
            asks = data.get("asks", []) if isinstance(data, dict) else []
            if bids and asks:
                bid = safe_float(bids[0][0], 0.0)
                ask = safe_float(asks[0][0], 0.0)
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
                bid_usdt = sum(safe_float(p) * safe_float(q) for p, q in bids[:10])
                ask_usdt = sum(safe_float(p) * safe_float(q) for p, q in asks[:10])
                denom = max(bid_usdt + ask_usdt, 1e-12)
                out = {
                    "spread_bps": (ask - bid) / mid * 10000 if mid > 0 else 9999.0,
                    "depth_usdt": min(bid_usdt, ask_usdt),
                    "imbalance": (bid_usdt - ask_usdt) / denom,
                }
        except Exception:
            pass
        self._depth_cache[symbol] = (time.time(), out)
        return out

    def adjust_signal_v7(self, sig: TradeSignal, entry: Sequence[Candle], trend: Sequence[Candle], regime: MarketRegime) -> TradeSignal:
        if sig.entry <= 0:
            return sig
        direction = sig.direction if sig.direction in {"LONG", "SHORT"} else ("LONG" if sig.tp2 > sig.entry else "SHORT")
        score = safe_float(sig.score, 0.0)
        reasons = list(sig.reasons)
        blockers = list(sig.blockers)
        # 清掉旧评分不足，V7 调整后统一重算。
        blockers = [b for b in blockers if not str(b).startswith("评分不足")]

        # 1) 波动状态：太冷没有空间，太热容易被清算插针。
        atr_vals = [x for x in atr_series(entry, 14) if x is not None and x > 0]
        if atr_vals:
            cur_atr = atr_vals[-1]
            pr = percentile_rank(atr_vals[-120:], cur_atr)
            if pr < env_float("VOL_RANK_TOO_LOW", 0.08):
                score -= 8
                reasons.append(f"波动分位过低 {pr:.0%}，空间不足")
            elif pr > env_float("VOL_RANK_TOO_HIGH", 0.96):
                blockers.append(f"波动分位极端 {pr:.0%}，插针/滑点风险高")
            elif 0.25 <= pr <= 0.85:
                score += 4
                reasons.append(f"波动分位健康 {pr:.0%}")

        # 2) 震荡过滤：趋势策略在高 choppiness 下容易反复止损。
        chop = choppiness_index(entry, env_int("CHOP_PERIOD", 14))
        if chop > env_float("MAX_CHOPPINESS", 64.0) and sig.setup != "breakout":
            blockers.append(f"震荡度过高 CHOP={chop:.1f}")
        elif chop > env_float("WARN_CHOPPINESS", 58.0):
            score -= 5
            reasons.append(f"震荡偏高 CHOP={chop:.1f}")
        elif chop < env_float("TREND_CHOPPINESS", 45.0):
            score += 4
            reasons.append(f"趋势质量较好 CHOP={chop:.1f}")

        # 3) 假突破/长影线过滤。
        if entry:
            lp = last_wick_profile(entry[-1])
            a = sig.atr or (atr(entry, 14) or 0.0)
            candle_range_atr = lp["range"] / a if a > 0 else 0.0
            if direction == "LONG":
                if lp["upper_wick_ratio"] > env_float("MAX_AGAINST_WICK_RATIO", 0.52) and lp["close_location"] < 0.70:
                    blockers.append(f"多头假突破风险：上影线 {lp['upper_wick_ratio']:.0%}")
                if candle_range_atr > env_float("MAX_SIGNAL_RANGE_ATR", 2.8) and lp["close_location"] < 0.55:
                    blockers.append(f"信号K过大且收盘不强 {candle_range_atr:.1f}ATR")
            else:
                if lp["lower_wick_ratio"] > env_float("MAX_AGAINST_WICK_RATIO", 0.52) and lp["close_location"] > 0.30:
                    blockers.append(f"空头假跌破风险：下影线 {lp['lower_wick_ratio']:.0%}")
                if candle_range_atr > env_float("MAX_SIGNAL_RANGE_ATR", 2.8) and lp["close_location"] > 0.45:
                    blockers.append(f"信号K过大且收盘不弱 {candle_range_atr:.1f}ATR")

        # 4) 本币 4H / 大周期对齐。
        htf = self.symbol_regime_candles(sig.symbol)
        if len(htf) >= 80:
            hclose = closes(htf)
            hp = hclose[-1]
            h20 = ema(hclose, 20) or hp
            h60 = ema(hclose, 60) or hp
            h200 = ema(hclose, 200) or h60
            if direction == "LONG":
                if hp > h60 and h20 > h60:
                    score += 7
                    reasons.append("本币大周期支持多头")
                elif hp < h200 and h20 < h60 and env_bool("BLOCK_COUNTER_HTF", True):
                    blockers.append("本币大周期空头，不做普通多头")
            else:
                if hp < h60 and h20 < h60:
                    score += 7
                    reasons.append("本币大周期支持空头")
                elif hp > h200 and h20 > h60 and env_bool("BLOCK_COUNTER_HTF", True):
                    blockers.append("本币大周期多头，不做普通空头")

        # 5) 盘口深度/短线买卖盘确认，只作为加减分或否决，不作为开仓核心。
        depth = self.depth_features(sig.symbol)
        min_depth = env_float("MIN_DEPTH_USDT", 0.0)
        if min_depth > 0 and safe_float(depth.get("depth_usdt"), 0.0) < min_depth:
            blockers.append(f"盘口深度不足 {fmt_num(safe_float(depth.get('depth_usdt'), 0.0))} USDT")
        spread_bps = safe_float(depth.get("spread_bps"), 9999.0)
        if spread_bps > env_float("MAX_EXEC_SPREAD_BPS", 10.0):
            blockers.append(f"执行点差过大 {spread_bps:.1f}bps")
        imb = safe_float(depth.get("imbalance"), 0.0)
        if direction == "LONG":
            if imb > 0.12:
                score += 3; reasons.append(f"盘口买盘略强 imbalance={imb:.2f}")
            elif imb < -0.25:
                score -= 5; reasons.append(f"盘口卖压偏强 imbalance={imb:.2f}")
        else:
            if imb < -0.12:
                score += 3; reasons.append(f"盘口卖盘略强 imbalance={imb:.2f}")
            elif imb > 0.25:
                score -= 5; reasons.append(f"盘口买盘偏强 imbalance={imb:.2f}")

        # 6) 成本压力：低 RR 的边缘单经不起手续费/滑点压力。
        cost_r = ((self.settings.fee_bps * 2 + self.settings.slippage_bps * 2) / 10000.0 * sig.entry) / max(abs(sig.entry - sig.stop_loss), 1e-12)
        if cost_r > env_float("MAX_COST_R", 0.18):
            blockers.append(f"交易成本占R过高 cost={cost_r:.2f}R")
        elif cost_r < 0.08:
            score += 2

        score = clamp(score, 0, 100)
        if score < self.settings.score_threshold:
            blockers.append(f"评分不足 {score:.1f} < {self.settings.score_threshold:.1f}")
        sig.direction = direction if not blockers else "NO_TRADE"
        sig.score = score
        # 去重保序，避免 prompt 太臃肿。
        sig.reasons = list(dict.fromkeys([str(x) for x in reasons if str(x).strip()]))[:16]
        sig.blockers = list(dict.fromkeys([str(x) for x in blockers if str(x).strip()]))[:16]
        return sig

    def analyze_symbol(self, symbol: str, quote_volume: float, regime: MarketRegime) -> TradeSignal:
        if not self.client:
            return self._no_trade(symbol, quote_volume, ["无 client"])
        try:
            trend = self.client.klines(symbol, self.settings.trend_interval, self.settings.kline_limit)
            entry = self.client.klines(symbol, self.settings.entry_interval, self.settings.kline_limit)
            fund = safe_float(self.client.premium_index(symbol).get("lastFundingRate"), 0.0)
            sig = self.analyze_symbol_from_candles(symbol, entry, trend, regime, quote_volume, fund)
            sig = self.adjust_signal_with_institutional_factors(sig, entry, trend, regime)
            return self.adjust_signal_v7(sig, entry, trend, regime)
        except Exception as exc:
            logging.debug("%s V7 分析失败: %s", symbol, exc)
            return self._no_trade(symbol, quote_volume, [f"分析异常: {exc}"])


class RiskManagerV7(RiskManager):
    """V7 风控：在不突破上限的前提下做置信度缩仓。"""

    def build_plan(self, signal: TradeSignal, equity: float, open_positions: Dict[str, Any], total_notional: float, cooldowns: Dict[str, str]) -> Tuple[Optional[OrderPlan], List[str]]:
        plan, blockers = super().build_plan(signal, equity, open_positions, total_notional, cooldowns)
        if not plan or not env_bool("USE_CONFIDENCE_POSITION_SIZING", True):
            return plan, blockers
        # 只允许缩小风险，不自动放大超过配置的 RISK_PER_TRADE。
        scale = 1.0
        if signal.score < self.settings.score_threshold + 5:
            scale *= 0.65
        elif signal.score < self.settings.score_threshold + 10:
            scale *= 0.80
        if "EdgeMemory 无历史样本" in " | ".join(signal.reasons):
            scale *= env_float("NO_EDGE_SIZE_SCALE", 0.70)
        if signal.atr_pct > self.settings.max_atr_pct * 0.75:
            scale *= 0.75
        scale = clamp(scale, env_float("MIN_POSITION_SCALE", 0.35), 1.0)
        if scale >= 0.999:
            return plan, blockers
        rule = self.rules.get(plan.symbol)
        if not rule:
            return plan, blockers
        new_qty = rule.qty_float(plan.qty * scale)
        new_notional = new_qty * plan.entry
        new_risk = plan.risk_usdt * (new_qty / max(plan.qty, 1e-12))
        if new_qty <= 0 or (rule.min_qty and new_qty < rule.min_qty) or (rule.min_notional and new_notional < rule.min_notional):
            return None, ["置信度缩仓后低于最小下单要求"]
        plan.qty = new_qty
        plan.notional = new_notional
        plan.risk_usdt = new_risk
        plan.reasons = list(plan.reasons) + [f"V7 置信度缩仓 scale={scale:.2f}"]
        return plan, []


class RiskGovernorV7(RiskGovernor):
    """AI 后的最终闸门：增加流程等级与实盘准备度。"""

    def approve(self, decision: AIDecision, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        match, notes = super().approve(decision, payload)
        if not match:
            return match, notes
        blockers: List[str] = []
        if env_bool("REQUIRE_PROCESS_TIER_FOR_TRADE", True):
            tier = str(match.get("process_tier", "D"))
            allowed = set(parse_csv_symbols(env_str("ALLOWED_PROCESS_TIERS", "A,B")))
            if tier not in allowed:
                blockers.append(f"流程等级不足 tier={tier}")
        if self.settings.trading_mode == "live" and not bool(match.get("live_ready", False)):
            blockers.append("live_ready=false，实盘拒绝")
        if blockers:
            return None, blockers
        return match, notes + ["V7_GOVERNOR_APPROVED"]


class BrainV7(BrainV6):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.scanner = MarketScannerV7(settings, self.client)
        self.strategy = StrategyEngineV7(settings, self.client)
        self.risk = RiskManagerV7(settings, self.rules)
        self.governor = RiskGovernorV7(settings)
        self.process_file = settings.workspace / "process_dashboard.json"
        self.daily_report_file = settings.workspace / "daily_report.md"

    def _candidate_tier(self, row: Dict[str, Any]) -> str:
        if not row.get("risk_approved"):
            return "D"
        score = safe_float(row.get("score"), 0.0)
        edge_n = int(row.get("edge_trades", 0) or 0)
        edge_exp = safe_float(row.get("edge_expectancy_R"), 0.0)
        pf = safe_float(row.get("edge_profit_factor"), 0.0)
        if score >= 90 and edge_n >= env_int("TIER_A_MIN_EDGE_TRADES", 20) and edge_exp >= env_float("TIER_A_MIN_EXPECTANCY_R", 0.08) and pf >= 1.25:
            return "A"
        if score >= 84 and (edge_n < 8 or edge_exp >= 0.0) and pf >= 1.0:
            return "B"
        if score >= self.settings.score_threshold:
            return "C"
        return "D"

    def _portfolio_corr_blockers(self, row: Dict[str, Any]) -> List[str]:
        if not env_bool("USE_PORTFOLIO_CORR_GUARD", True):
            return []
        positions = self.broker.positions()
        if not positions:
            return []
        sym = str(row.get("symbol", ""))
        direction = str(row.get("direction", ""))
        blockers: List[str] = []
        try:
            c1 = self.client.klines(sym, self.settings.trend_interval, min(self.settings.kline_limit, 160))
            for psym, pos in positions.items():
                if psym == sym:
                    blockers.append("已有同币持仓")
                    continue
                pdir = str(pos.get("direction", ""))
                if pdir and pdir != direction:
                    continue
                pc = self.client.klines(psym, self.settings.trend_interval, min(self.settings.kline_limit, 160))
                corr = corr_returns(c1, pc, env_int("PORTFOLIO_CORR_BARS", 80))
                if corr >= env_float("MAX_PORTFOLIO_CORR", 0.82):
                    blockers.append(f"与持仓 {psym} 同向相关性过高 corr={corr:.2f}")
        except Exception:
            pass
        return blockers[:3]

    def build_ai_payload(self, regime: MarketRegime, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> Dict[str, Any]:
        payload = super().build_ai_payload(regime, signals, marks)
        payload["schema"] = "brain_v7_ai_candidates_v1"
        payload["process"] = {
            "version": "V7",
            "gates": [
                "market_regime",
                "liquidity_spread",
                "data_quality",
                "relative_strength",
                "derivatives_crowding",
                "volatility_chop_wick",
                "news_event_risk",
                "edge_memory",
                "portfolio_correlation",
                "risk_budget",
                "ai_governor",
            ],
            "principle": "AI 只能在量化和风控通过的候选里排序；不能提高仓位、不能移动止损、不能绕过 live_ready。",
        }
        for row in payload.get("candidates", []):
            corr_blockers = self._portfolio_corr_blockers(row) if row.get("risk_approved") else []
            if corr_blockers:
                row["risk_approved"] = False
                row.setdefault("risk_blockers", []).extend(corr_blockers)
                row["ai_priority"] = safe_float(row.get("ai_priority"), 0.0) - 30.0
            tier = self._candidate_tier(row)
            row["process_tier"] = tier
            row["live_ready"] = bool(row.get("risk_approved")) and tier in {"A", "B"} and not row.get("risk_blockers")
            row["pre_trade_checklist"] = {
                "risk_approved": bool(row.get("risk_approved")),
                "score_ok": safe_float(row.get("score"), 0.0) >= self.settings.score_threshold,
                "rr_ok": safe_float(row.get("rr"), 0.0) >= self.settings.min_rr,
                "edge_ok_or_unknown": int(row.get("edge_trades", 0) or 0) < 8 or safe_float(row.get("edge_expectancy_R"), 0.0) >= 0,
                "no_blockers": not row.get("blockers") and not row.get("risk_blockers"),
                "tier": tier,
            }
        payload["candidates"].sort(key=lambda r: (str(r.get("process_tier", "D")) in {"A", "B"}, bool(r.get("risk_approved")), safe_float(r.get("ai_priority"), 0.0)), reverse=True)
        _atomic_write_json(self.ai_candidates_file, payload)
        self._write_aipro_prompt(payload)
        self._write_process_dashboard(payload)
        return payload

    def _write_aipro_prompt(self, payload: Dict[str, Any]) -> None:
        top = payload.get("candidates", [])[: env_int("AI_PROMPT_TOP_N", 10)]
        slim = {"schema": payload.get("schema"), "time": payload.get("time"), "mode": payload.get("mode"), "regime": payload.get("regime"), "account": payload.get("account"), "policy": payload.get("policy"), "process": payload.get("process"), "candidates": top}
        prompt = f"""# Brain V8 / Ai Pro 机构级决策任务

你是合约量化交易风控代理。你的任务不是预测暴涨暴跌，而是在量化系统已经筛出的候选中，选择是否执行一笔最有正期望的交易。

硬规则：
1. 只能从 candidates 中选择，不能创造 symbol。
2. 不允许修改 entry、stop_loss、tp1、tp2、qty、risk_usdt。
3. 只能选择 risk_approved=true 且 live_ready=true 的候选；paper 模式也应优先选择 process_tier=A/B。
4. 如果所有候选 process_tier 为 C/D，或存在 risk_blockers/blockers，输出 NO_TRADE。
5. 如果大盘 regime 为 CHAOS，除非候选明确属于 A 级且风控通过，否则输出 NO_TRADE。
6. 输出必须是 JSON，不要输出散文。

输出格式：
```json
{{
  "decision": "TRADE" 或 "NO_TRADE",
  "symbol": "例如 SOLUSDT，NO_TRADE 时留空",
  "direction": "LONG 或 SHORT，NO_TRADE 时留空",
  "confidence": 0.0到1.0,
  "reason": ["选择或拒绝原因"],
  "risk_notes": ["你观察到的风险"]
}}
```

候选数据：
```json
{json.dumps(slim, ensure_ascii=False, indent=2)}
```
"""
        self.ai_prompt_file.write_text(prompt, encoding="utf-8")

    def _write_process_dashboard(self, payload: Dict[str, Any]) -> None:
        rows = payload.get("candidates", [])
        counts: Dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
        for r in rows:
            t = str(r.get("process_tier", "D"))
            counts[t] = counts.get(t, 0) + 1
        obj = {
            "time": payload.get("time"),
            "mode": payload.get("mode"),
            "regime": payload.get("regime"),
            "tier_counts": counts,
            "approved": sum(1 for r in rows if r.get("risk_approved")),
            "live_ready": sum(1 for r in rows if r.get("live_ready")),
            "top": rows[:10],
            "notes": [
                "A/B 候选才适合进入 AI 最终选择；C 仅观察；D 拒绝。",
                "如果长期没有 A/B，不要降低风控阈值，先看 edge_memory 与 metrics。",
            ],
        }
        _atomic_write_json(self.process_file, obj)

    def write_daily_report(self) -> Path:
        rows: List[Dict[str, str]] = []
        if self.settings.journal_file.exists():
            try:
                with self.settings.journal_file.open("r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                rows = []
        today = utc_day()
        today_rows = [r for r in rows if str(r.get("time", "")).startswith(today)]
        closes = [r for r in today_rows if r.get("event") in {"STOP", "TP1", "TP2"}]
        pnl = sum(safe_float(r.get("pnl"), 0.0) for r in closes)
        wins = sum(1 for r in closes if safe_float(r.get("pnl"), 0.0) > 0)
        losses = sum(1 for r in closes if safe_float(r.get("pnl"), 0.0) < 0)
        open_pos = self.broker.positions()
        text = f"""# Brain V8 Daily Report

- 日期：{today} UTC
- 模式：{self.settings.trading_mode}
- 今日已结算事件数：{len(closes)}
- 今日胜/负：{wins}/{losses}
- 今日已结算 PnL：{fmt_num(pnl)} USDT
- 当前持仓数：{len(open_pos)}
- 工作目录：`{self.settings.workspace}`

## 当前持仓

```json
{json.dumps(open_pos, ensure_ascii=False, indent=2)}
```

## 下一步检查

1. 查看 `metrics.json` 的 expectancy_R 和 max_drawdown_R。
2. 查看 `edge_memory.json` 是否有足够样本支持当前 setup。
3. 查看 `process_dashboard.json` 中 A/B/C/D 候选比例。
4. 如果 paper 结果与回测差距大，优先检查滑点、手续费、成交延迟和信号收盘确认。
"""
        self.daily_report_file.write_text(text, encoding="utf-8")
        return self.daily_report_file



# =============================================================================
# V8：机构级缺口补齐层（成本模型、组合风险、监控熔断、参数治理、执行审计）
# =============================================================================

import random


def _read_json_safe(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.debug("读取 JSON 失败: %s", path)
    return default


def _rolling_max_drawdown(values: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in values:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def losing_streak(values: Sequence[float]) -> int:
    cur = best = 0
    for r in values:
        if r <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def monte_carlo_metrics(values: Sequence[float], sims: int = 1000, ruin_dd_R: float = 12.0) -> Dict[str, Any]:
    """交易序列扰动：用 bootstrap 估计尾部回撤和破产风险。

    注意：这不是收益保证，只是检验策略对交易顺序和样本扰动是否脆弱。
    """
    rs = [safe_float(x) for x in values if math.isfinite(safe_float(x))]
    if not rs:
        return {"sims": 0, "p05_total_R": 0.0, "p50_total_R": 0.0, "p95_max_drawdown_R": 0.0, "risk_of_ruin": 0.0}
    sims = max(50, int(sims))
    totals: List[float] = []
    dds: List[float] = []
    ruins = 0
    n = len(rs)
    for _ in range(sims):
        sample = [random.choice(rs) for _ in range(n)]
        total = sum(sample)
        dd = _rolling_max_drawdown(sample)
        totals.append(total)
        dds.append(dd)
        if dd >= ruin_dd_R or total <= -ruin_dd_R:
            ruins += 1
    totals.sort(); dds.sort()
    def q(arr: List[float], p: float) -> float:
        if not arr:
            return 0.0
        idx = int(clamp(p, 0, 1) * (len(arr) - 1))
        return arr[idx]
    return {
        "sims": sims,
        "sample_trades": n,
        "p05_total_R": round(q(totals, 0.05), 4),
        "p50_total_R": round(q(totals, 0.50), 4),
        "p95_total_R": round(q(totals, 0.95), 4),
        "p50_max_drawdown_R": round(q(dds, 0.50), 4),
        "p95_max_drawdown_R": round(q(dds, 0.95), 4),
        "risk_of_ruin": round(ruins / sims, 4),
        "ruin_dd_R": ruin_dd_R,
    }


def compute_institutional_metrics(trades: Sequence[BacktestTrade]) -> Dict[str, Any]:
    base = compute_metrics(trades)
    rs = [safe_float(t.r_multiple) for t in trades]
    n = len(rs)
    neg = [r for r in rs if r < 0]
    pos = [r for r in rs if r > 0]
    downside = statistics.pstdev([min(0.0, r) for r in rs]) if len(rs) > 1 else 0.0
    exp = sum(rs) / n if n else 0.0
    sortino = exp / downside * math.sqrt(max(n, 1)) if downside > 0 else 0.0
    max_dd = safe_float(base.get("max_drawdown_R"), 0.0)
    calmar = safe_float(base.get("total_R"), 0.0) / max_dd if max_dd > 0 else 0.0
    tail_loss = sorted(rs)[int(0.05 * (n - 1))] if n else 0.0
    mc = monte_carlo_metrics(rs, env_int("MC_SIMS", 1000), env_float("RUIN_DD_R", 12.0))
    base.update({
        "engine_version": "V8",
        "median_R": round(statistics.median(rs), 4) if rs else 0.0,
        "sortino_like": round(sortino, 4),
        "calmar_like": round(calmar, 4),
        "tail_5pct_R": round(tail_loss, 4),
        "max_losing_streak": losing_streak(rs),
        "avg_positive_R": round(sum(pos) / len(pos), 4) if pos else 0.0,
        "avg_negative_R": round(sum(neg) / len(neg), 4) if neg else 0.0,
        "monte_carlo": mc,
        "quality_gate": {
            "min_trades_ok": n >= env_int("V8_MIN_BACKTEST_TRADES", 80),
            "expectancy_ok": safe_float(base.get("expectancy_R"), 0.0) >= env_float("V8_MIN_EXPECTANCY_R", 0.03),
            "profit_factor_ok": safe_float(base.get("profit_factor"), 0.0) >= env_float("V8_MIN_PROFIT_FACTOR", 1.15),
            "max_dd_ok": max_dd <= env_float("V8_MAX_DD_R", 18.0),
            "mc_ruin_ok": safe_float(mc.get("risk_of_ruin"), 1.0) <= env_float("V8_MAX_RISK_OF_RUIN", 0.20),
        },
    })
    q = base["quality_gate"]
    base["quality_gate"]["passed"] = all(bool(v) for k, v in q.items() if k != "passed")
    return base


class BacktesterV8(Backtester):
    """V8 回测：在 V7 成本基础上增加资金费率、延迟滑点和不利选择压力项。"""

    def _simulate_trade(self, symbol: str, sig: TradeSignal, candles: Sequence[Candle], entry_i: int, st: Settings) -> Optional[BacktestTrade]:
        trade = super()._simulate_trade(symbol, sig, candles, entry_i, st)
        if not trade:
            return None
        risk = abs(trade.entry - trade.stop_loss)
        if risk <= 0:
            return trade
        hold_hours = max(0.0, (trade.exit_time - trade.entry_time) / 3_600_000.0)
        # 资金费率方向近似：做多付正费率、做空付负费率。由于历史逐笔 funding 未拉取，这里用保守配置估计。
        funding_bps_8h = env_float("BACKTEST_FUNDING_BPS_PER_8H", 0.0)
        latency_bps = env_float("LATENCY_SLIPPAGE_BPS", 0.5)
        adverse_bps = env_float("ADVERSE_SELECTION_BPS", 0.5 if "STOP" in trade.reason else 0.2)
        funding_cost_price = trade.entry * (funding_bps_8h / 10000.0) * (hold_hours / 8.0)
        extra_cost_price = trade.entry * ((latency_bps + adverse_bps) / 10000.0)
        extra_r = (funding_cost_price + extra_cost_price) / risk
        if extra_r > 0:
            trade.r_multiple -= extra_r
            trade.pnl_pct = trade.r_multiple * (risk / max(trade.entry, 1e-12)) * 100.0
            trade.reason += f" | V8_cost funding_bps8h={funding_bps_8h:.3f} latency_bps={latency_bps:.2f} adverse_bps={adverse_bps:.2f} cost_R={extra_r:.4f}"
        return trade


class SystemCircuitBreaker:
    """全局熔断器：把配置、日内损失、连续异常、手动 kill switch 合并成开仓前最后一道闸门。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.workspace / "system_risk_state.json"
        self.store = JsonStore(self.path, {"day": utc_day(), "api_errors": 0, "order_errors": 0, "manual_halt": False, "last_error": "", "halt_until": ""})
        self.state = self.store.load()

    def reset_day_if_needed(self) -> None:
        if self.state.get("day") != utc_day():
            self.state.update({"day": utc_day(), "api_errors": 0, "order_errors": 0, "last_error": "", "halt_until": ""})
            self.store.save(self.state)

    def record_error(self, kind: str, message: str) -> None:
        self.reset_day_if_needed()
        key = "order_errors" if kind == "order" else "api_errors"
        self.state[key] = int(self.state.get(key, 0) or 0) + 1
        self.state["last_error"] = str(message)[-500:]
        if self.state[key] >= env_int("MAX_ERRORS_BEFORE_HALT", 5):
            minutes = env_int("ERROR_HALT_MINUTES", 60)
            self.state["halt_until"] = (utc_now() + dt.timedelta(minutes=minutes)).isoformat()
        self.store.save(self.state)

    def manual_halt(self, enabled: bool = True) -> None:
        self.state["manual_halt"] = bool(enabled)
        self.store.save(self.state)

    def blockers(self) -> List[str]:
        self.reset_day_if_needed()
        out: List[str] = []
        if env_bool("GLOBAL_KILL_SWITCH", False) or env_bool("NO_NEW_TRADES", False):
            out.append("GLOBAL_KILL_SWITCH/NO_NEW_TRADES 已开启")
        if bool(self.state.get("manual_halt")):
            out.append("system_risk_state manual_halt=true")
        raw = str(self.state.get("halt_until") or "")
        if raw:
            try:
                until = dt.datetime.fromisoformat(raw)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                if utc_now() < until:
                    out.append(f"错误熔断中，halt_until={raw}")
            except Exception:
                pass
        if int(self.state.get("api_errors", 0) or 0) >= env_int("MAX_API_ERRORS_PER_DAY", 20):
            out.append("日内 API 错误过多")
        if int(self.state.get("order_errors", 0) or 0) >= env_int("MAX_ORDER_ERRORS_PER_DAY", 3):
            out.append("日内订单错误过多")
        return out


class HealthMonitor:
    def __init__(self, settings: Settings, client: BinanceFuturesClient, broker: Any):
        self.settings = settings
        self.client = client
        self.broker = broker
        self.path = settings.workspace / "healthcheck.json"

    def snapshot(self, include_api: bool = True) -> Dict[str, Any]:
        checks: Dict[str, Any] = {
            "time": utc_now().isoformat(),
            "mode": self.settings.trading_mode,
            "workspace": str(self.settings.workspace),
            "files": {},
            "config": {},
            "api": {},
            "broker": {},
            "status": "OK",
            "blockers": [],
        }
        for name, path in {
            "journal": self.settings.journal_file,
            "metrics": self.settings.metrics_file,
            "backtest": self.settings.backtest_file,
            "log": self.settings.log_file,
            "state": self.settings.state_file,
        }.items():
            checks["files"][name] = {"path": str(path), "exists": path.exists(), "size": path.stat().st_size if path.exists() else 0}
        checks["config"] = {
            "risk_per_trade": self.settings.risk_per_trade,
            "max_positions": self.settings.max_positions,
            "daily_max_loss_pct": self.settings.daily_max_loss_pct,
            "score_threshold": self.settings.score_threshold,
            "min_rr": self.settings.min_rr,
            "use_exchange_protection": self.settings.use_exchange_protection,
            "live_confirmed": env_str("LIVE_TRADING_CONFIRM", "").upper() == "YES",
        }
        if self.settings.trading_mode == "live":
            if not checks["config"]["live_confirmed"]:
                checks["blockers"].append("live 未设置 LIVE_TRADING_CONFIRM=YES")
            if not self.settings.use_exchange_protection:
                checks["blockers"].append("live 未启用 USE_EXCHANGE_PROTECTION")
            if self.settings.risk_per_trade > env_float("LIVE_MAX_RISK_PER_TRADE", 0.005):
                checks["blockers"].append("live risk_per_trade 超过 LIVE_MAX_RISK_PER_TRADE")
        try:
            positions = self.broker.positions()
            checks["broker"]["positions"] = len(positions)
            checks["broker"]["equity"] = self.broker.equity({})
        except Exception as exc:
            checks["blockers"].append(f"broker 检查失败: {exc}")
        if include_api:
            try:
                server = self.client.public("GET", "/fapi/v1/time")
                server_time = int(server.get("serverTime", 0))
                drift_ms = abs(now_ms() - server_time) if server_time else 0
                checks["api"] = {"server_time": server_time, "clock_drift_ms": drift_ms, "ok": drift_ms < env_int("MAX_CLOCK_DRIFT_MS", 5000)}
                if drift_ms >= env_int("MAX_CLOCK_DRIFT_MS", 5000):
                    checks["blockers"].append(f"本地时钟偏差过大 {drift_ms}ms")
            except Exception as exc:
                checks["api"] = {"ok": False, "error": str(exc)}
                checks["blockers"].append(f"API 连通性失败: {exc}")
        if checks["blockers"]:
            checks["status"] = "BLOCKED"
        _atomic_write_json(self.path, checks)
        return checks


class ExecutionAuditor:
    def __init__(self, settings: Settings, broker: Any):
        self.settings = settings
        self.broker = broker
        self.path = settings.workspace / "execution_audit.json"

    def reconcile(self) -> Dict[str, Any]:
        rows: List[Dict[str, str]] = []
        if self.settings.journal_file.exists():
            try:
                with self.settings.journal_file.open("r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                rows = []
        opens = [r for r in rows if str(r.get("event", "")).endswith("OPEN") or r.get("event") == "OPEN"]
        exits = [r for r in rows if r.get("event") in {"STOP", "TP1", "TP2", "EXCHANGE_CLOSE"}]
        slippage_rows = []
        for r in opens[-200:]:
            intended = safe_float(r.get("entry"), 0.0)
            actual = safe_float(r.get("price"), intended)
            if intended > 0 and actual > 0:
                slippage_rows.append((actual - intended) / intended * 10000.0)
        obj = {
            "time": utc_now().isoformat(),
            "mode": self.settings.trading_mode,
            "journal_rows": len(rows),
            "open_events": len(opens),
            "exit_events": len(exits),
            "current_positions": self.broker.positions(),
            "avg_open_slippage_bps": round(sum(slippage_rows) / len(slippage_rows), 4) if slippage_rows else 0.0,
            "max_abs_open_slippage_bps": round(max(abs(x) for x in slippage_rows), 4) if slippage_rows else 0.0,
            "notes": [
                "paper 模式下 slippage 来自配置模型；demo/live 下需要对比交易所成交回报。",
                "若实际滑点长期高于回测模型，应上调 SLIPPAGE_BPS / LATENCY_SLIPPAGE_BPS。",
            ],
        }
        _atomic_write_json(self.path, obj)
        return obj


class PortfolioRiskEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sector_map = _read_json_safe(Path(env_str("SECTOR_MAP_FILE", str(settings.workspace / "sector_map.json"))).expanduser(), {})

    def sector_for(self, symbol: str) -> str:
        if isinstance(self.sector_map, dict) and symbol in self.sector_map:
            return str(self.sector_map[symbol])
        base = symbol.replace("USDT", "")
        # 粗略默认分组，用户可通过 sector_map.json 覆盖。
        l1 = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"}
        if base in l1:
            return "LARGE_CAP"
        if base in {"OP", "ARB", "STRK", "MANTA", "METIS"}:
            return "L2"
        if base in {"UNI", "AAVE", "CRV", "COMP", "MKR", "SUSHI"}:
            return "DEFI"
        if base in {"PEPE", "WIF", "BONK", "SHIB", "FLOKI"}:
            return "MEME"
        if base in {"RNDR", "FET", "TAO", "AI", "AGIX", "WLD"}:
            return "AI"
        return "ALT"

    def candidate_blockers(self, row: Dict[str, Any], open_positions: Dict[str, Any]) -> List[str]:
        blockers: List[str] = []
        if not open_positions:
            return blockers
        direction = str(row.get("direction", ""))
        sym = str(row.get("symbol", ""))
        sector = self.sector_for(sym)
        same_dir = 0
        same_sector = 0
        for psym, pos in open_positions.items():
            pdir = str(pos.get("direction", pos.get("positionSide", "")))
            # Binance live row 可能只有 positionAmt；按符号推断方向。
            if not pdir or pdir not in {"LONG", "SHORT"}:
                amt = safe_float(pos.get("positionAmt"), 0.0) if isinstance(pos, dict) else 0.0
                pdir = "LONG" if amt > 0 else "SHORT" if amt < 0 else ""
            if pdir == direction:
                same_dir += 1
            if self.sector_for(str(psym)) == sector:
                same_sector += 1
        if same_dir >= env_int("MAX_SAME_DIRECTION_POSITIONS", 2):
            blockers.append("同方向持仓数量达到组合上限")
        if same_sector >= env_int("MAX_SAME_SECTOR_POSITIONS", 1):
            blockers.append(f"同板块 {sector} 持仓达到上限")
        return blockers

    def exposure_report(self, positions: Dict[str, Any]) -> Dict[str, Any]:
        by_sector: Dict[str, int] = {}
        by_direction: Dict[str, int] = {"LONG": 0, "SHORT": 0, "UNKNOWN": 0}
        for sym, pos in positions.items():
            by_sector[self.sector_for(str(sym))] = by_sector.get(self.sector_for(str(sym)), 0) + 1
            amt = safe_float(pos.get("positionAmt"), 0.0) if isinstance(pos, dict) else 0.0
            direction = str(pos.get("direction", "")) if isinstance(pos, dict) else ""
            if direction not in {"LONG", "SHORT"}:
                direction = "LONG" if amt > 0 else "SHORT" if amt < 0 else "UNKNOWN"
            by_direction[direction] = by_direction.get(direction, 0) + 1
        return {"positions": len(positions), "by_sector": by_sector, "by_direction": by_direction}


class BrainV8(BrainV7):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.backtester = BacktesterV8(settings, self.client)
        self.circuit = SystemCircuitBreaker(settings)
        self.health = HealthMonitor(settings, self.client, self.broker)
        self.auditor = ExecutionAuditor(settings, self.broker)
        self.portfolio_risk = PortfolioRiskEngine(settings)
        self.v8_dashboard_file = settings.workspace / "v8_risk_dashboard.json"
        self.cost_report_file = settings.workspace / "cost_model_report.json"
        self.parameter_report_file = settings.workspace / "parameter_governance.json"

    def build_ai_payload(self, regime: MarketRegime, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> Dict[str, Any]:
        payload = super().build_ai_payload(regime, signals, marks)
        payload["schema"] = "brain_v8_ai_candidates_v1"
        payload["process"]["version"] = "V8"
        payload["process"]["gates"].extend([
            "transaction_cost_model",
            "monte_carlo_robustness",
            "sector_exposure",
            "global_circuit_breaker",
            "execution_audit",
            "parameter_governance",
        ])
        open_positions = self.broker.positions()
        circuit_blockers = self.circuit.blockers()
        metrics = _read_json_safe(self.settings.metrics_file, {})
        gate = metrics.get("quality_gate", {}) if isinstance(metrics, dict) else {}
        require_quality = env_bool("REQUIRE_BACKTEST_QUALITY_GATE", False if self.settings.trading_mode == "paper" else True)
        for row in payload.get("candidates", []):
            row["sector"] = self.portfolio_risk.sector_for(str(row.get("symbol", "")))
            row.setdefault("risk_blockers", [])
            if circuit_blockers:
                row["risk_approved"] = False
                row["risk_blockers"].extend(circuit_blockers)
            for b in self.portfolio_risk.candidate_blockers(row, open_positions):
                row["risk_approved"] = False
                row["risk_blockers"].append(b)
            # 成本调整：若 edge 很薄而成本模型较重，则不进实盘。
            edge = safe_float(row.get("edge_expectancy_R"), 0.0)
            cost_floor = env_float("MIN_EDGE_AFTER_COST_R", 0.02)
            edge_trades = int(row.get("edge_trades", 0) or 0)
            if edge_trades >= env_int("MIN_EDGE_TRADES_FOR_COST_GATE", 20) and edge < cost_floor:
                row["risk_approved"] = False
                row["risk_blockers"].append(f"成本后 Edge 不足 edge_R={edge:.4f} < {cost_floor:.4f}")
            if require_quality and not bool(gate.get("passed", False)):
                row["risk_approved"] = False
                row["risk_blockers"].append("全局回测质量门未通过，拒绝新仓")
            # V8 live 进一步收紧：默认只允许 A 级。
            if self.settings.trading_mode == "live" and env_bool("LIVE_REQUIRE_TIER_A", True) and str(row.get("process_tier")) != "A":
                row["risk_approved"] = False
                row["risk_blockers"].append("live 要求 process_tier=A")
            row["live_ready"] = bool(row.get("risk_approved")) and str(row.get("process_tier")) in set(parse_csv_symbols(env_str("LIVE_ALLOWED_PROCESS_TIERS", "A,B")))
            # 重新计算 AI 优先级，强惩罚风险和薄 edge。
            if row.get("risk_blockers"):
                row["ai_priority"] = safe_float(row.get("ai_priority"), 0.0) - 50.0
            else:
                row["ai_priority"] = safe_float(row.get("ai_priority"), 0.0) + clamp(edge * 20.0, -5.0, 8.0)
        payload["portfolio"] = self.portfolio_risk.exposure_report(open_positions)
        payload["circuit_breaker"] = {"blockers": circuit_blockers, "state": self.circuit.state}
        payload["backtest_quality_gate"] = gate
        payload["candidates"].sort(key=lambda r: (bool(r.get("risk_approved")), str(r.get("process_tier", "D")) == "A", safe_float(r.get("ai_priority"), 0.0)), reverse=True)
        _atomic_write_json(self.ai_candidates_file, payload)
        self._write_aipro_prompt(payload)
        self._write_process_dashboard(payload)
        self.write_v8_risk_dashboard(payload)
        return payload

    def _write_aipro_prompt(self, payload: Dict[str, Any]) -> None:
        top = payload.get("candidates", [])[: env_int("AI_PROMPT_TOP_N", 10)]
        slim = {"schema": payload.get("schema"), "time": payload.get("time"), "mode": payload.get("mode"), "regime": payload.get("regime"), "account": payload.get("account"), "portfolio": payload.get("portfolio"), "circuit_breaker": payload.get("circuit_breaker"), "backtest_quality_gate": payload.get("backtest_quality_gate"), "policy": payload.get("policy"), "process": payload.get("process"), "candidates": top}
        prompt = f"""# Brain V8 / Ai Pro 机构级决策任务

你是合约量化交易风控代理。你的任务不是预测涨跌，而是在量化系统、历史 Edge、组合风险、成本模型和熔断器均允许的候选里，选择是否执行一笔最有正期望的交易。

硬规则：
1. 只能从 candidates 中选择，不能创造 symbol。
2. 不允许修改 entry、stop_loss、tp1、tp2、qty、risk_usdt。
3. 只能选择 risk_approved=true 且 live_ready=true 的候选；paper 模式也应优先选择 process_tier=A/B。
4. 如果 circuit_breaker.blockers 非空，输出 NO_TRADE。
5. 如果 backtest_quality_gate.passed=false 且当前不是研究模式，输出 NO_TRADE。
6. 如果候选存在 risk_blockers/blockers，输出 NO_TRADE。
7. 不确定时输出 NO_TRADE。
8. 输出必须是 JSON，不要输出散文。

输出格式：
```json
{{
  "decision": "TRADE" 或 "NO_TRADE",
  "symbol": "例如 SOLUSDT，NO_TRADE 时留空",
  "direction": "LONG 或 SHORT，NO_TRADE 时留空",
  "confidence": 0.0到1.0,
  "reason": ["选择或拒绝原因"],
  "risk_notes": ["你观察到的风险"]
}}
```

候选数据：
```json
{json.dumps(slim, ensure_ascii=False, indent=2)}
```
"""
        self.ai_prompt_file.write_text(prompt, encoding="utf-8")

    def run_once(self, execute: bool = True) -> List[TradeSignal]:
        if self.circuit.blockers():
            regime, signals = self.scan()
            marks = self.mark_prices_for(signals)
            try:
                self.broker.manage_positions(marks)
            except Exception as exc:
                self.circuit.record_error("api", str(exc))
            logging.warning("V8 熔断器阻止新开仓: %s", " | ".join(self.circuit.blockers()))
            self.build_ai_payload(regime, signals, marks)
            return signals
        try:
            signals = super().run_once(execute=execute)
            self.health.snapshot(include_api=False)
            self.auditor.reconcile()
            return signals
        except Exception as exc:
            self.circuit.record_error("api", str(exc))
            raise

    def run_backtest(self) -> Tuple[List[BacktestTrade], Dict[str, Any]]:
        trades = self.backtester.backtest_many(self.universe())
        metrics = compute_institutional_metrics(trades)
        write_backtest_csv(self.settings.backtest_file, trades)
        self.settings.metrics_file.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        self.write_cost_model_report(trades, metrics)
        return trades, metrics

    def write_cost_model_report(self, trades: Sequence[BacktestTrade], metrics: Optional[Dict[str, Any]] = None) -> Path:
        metrics = metrics or compute_institutional_metrics(trades)
        obj = {
            "time": utc_now().isoformat(),
            "version": "V8",
            "fee_bps": self.settings.fee_bps,
            "slippage_bps": self.settings.slippage_bps,
            "latency_slippage_bps": env_float("LATENCY_SLIPPAGE_BPS", 0.5),
            "adverse_selection_bps": env_float("ADVERSE_SELECTION_BPS", 0.5),
            "backtest_funding_bps_per_8h": env_float("BACKTEST_FUNDING_BPS_PER_8H", 0.0),
            "metrics_summary": {k: metrics.get(k) for k in ["trades", "expectancy_R", "profit_factor", "max_drawdown_R", "quality_gate", "monte_carlo"]},
            "recommendation": "如果 paper/live 实际滑点高于这里的模型，优先提高 SLIPPAGE_BPS/LATENCY_SLIPPAGE_BPS 后重新回测。",
        }
        _atomic_write_json(self.cost_report_file, obj)
        return self.cost_report_file

    def write_v8_risk_dashboard(self, payload: Optional[Dict[str, Any]] = None) -> Path:
        payload = payload or _read_json_safe(self.ai_candidates_file, {})
        obj = {
            "time": utc_now().isoformat(),
            "mode": self.settings.trading_mode,
            "circuit_blockers": self.circuit.blockers(),
            "portfolio": self.portfolio_risk.exposure_report(self.broker.positions()),
            "health_file": str(self.health.path),
            "execution_audit_file": str(self.auditor.path),
            "metrics_file": str(self.settings.metrics_file),
            "quality_gate": _read_json_safe(self.settings.metrics_file, {}).get("quality_gate", {}),
            "candidate_counts": {
                "total": len(payload.get("candidates", [])) if isinstance(payload, dict) else 0,
                "risk_approved": sum(1 for r in payload.get("candidates", []) if r.get("risk_approved")) if isinstance(payload, dict) else 0,
                "live_ready": sum(1 for r in payload.get("candidates", []) if r.get("live_ready")) if isinstance(payload, dict) else 0,
            },
        }
        _atomic_write_json(self.v8_dashboard_file, obj)
        return self.v8_dashboard_file

    def run_healthcheck(self) -> Dict[str, Any]:
        obj = self.health.snapshot(include_api=True)
        obj["circuit_blockers"] = self.circuit.blockers()
        _atomic_write_json(self.health.path, obj)
        return obj

    def run_reconcile(self) -> Dict[str, Any]:
        return self.auditor.reconcile()

    def run_stress_test(self) -> Dict[str, Any]:
        trades: List[BacktestTrade] = []
        if self.settings.backtest_file.exists():
            try:
                with self.settings.backtest_file.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        trades.append(BacktestTrade(
                            symbol=row.get("symbol", ""), direction=row.get("direction", ""), setup=row.get("setup", ""),
                            entry_time=iso_to_ms(row.get("entry_time", "")) or 0, exit_time=iso_to_ms(row.get("exit_time", "")) or 0,
                            entry=safe_float(row.get("entry")), exit=safe_float(row.get("exit")), stop_loss=safe_float(row.get("stop_loss")),
                            tp1=safe_float(row.get("tp1")), tp2=safe_float(row.get("tp2")), r_multiple=safe_float(row.get("r_multiple")),
                            pnl_pct=safe_float(row.get("pnl_pct")), bars=int(safe_float(row.get("bars"), 0)), score=safe_float(row.get("score")), reason=row.get("reason", "")))
            except Exception:
                trades = []
        if not trades:
            trades, _ = self.run_backtest()
        metrics = compute_institutional_metrics(trades)
        out = {"time": utc_now().isoformat(), "metrics": metrics, "note": "Monte Carlo 使用交易 R 倍数 bootstrap，衡量顺序扰动下的尾部回撤。"}
        _atomic_write_json(self.settings.workspace / "stress_test.json", out)
        return out

    def write_parameter_governance(self) -> Path:
        metrics = _read_json_safe(self.settings.metrics_file, {})
        obj = {
            "time": utc_now().isoformat(),
            "current_params": {
                "score_threshold": self.settings.score_threshold,
                "min_rr": self.settings.min_rr,
                "atr_sl_mult": self.settings.atr_sl_mult,
                "tp2_r": self.settings.tp2_r,
                "risk_per_trade": self.settings.risk_per_trade,
            },
            "quality_gate": metrics.get("quality_gate", {}) if isinstance(metrics, dict) else {},
            "rules": [
                "参数只能在 walk-forward 测试集正期望时上线。",
                "如果测试集交易数低于 V8_MIN_BACKTEST_TRADES，不允许因单次高收益提高风险。",
                "实盘参数变更应先 paper 运行至少 N 笔，N 由 MIN_PAPER_TRADES_BEFORE_LIVE 控制。",
                "AI 只能选择候选，不能提高仓位或放宽止损。",
            ],
            "suggested_next_actions": [],
        }
        gate = obj["quality_gate"]
        if gate and not gate.get("passed"):
            obj["suggested_next_actions"].append("质量门未通过：降低交易频率、提高 score_threshold 或剔除 edge_memory 中负期望 setup。")
        if safe_float(metrics.get("max_drawdown_R"), 0.0) > env_float("V8_MAX_DD_R", 18.0):
            obj["suggested_next_actions"].append("回撤过大：降低 RISK_PER_TRADE、减少 max_positions、提高 MIN_RR。")
        if safe_float(metrics.get("profit_factor"), 0.0) < env_float("V8_MIN_PROFIT_FACTOR", 1.15):
            obj["suggested_next_actions"].append("Profit Factor 不足：检查手续费/滑点、资金费率与假突破过滤。")
        _atomic_write_json(self.parameter_report_file, obj)
        return self.parameter_report_file

    def write_daily_report(self) -> Path:
        super().write_daily_report()
        health = _read_json_safe(self.health.path, {})
        audit = _read_json_safe(self.auditor.path, {})
        dash = _read_json_safe(self.v8_dashboard_file, {})
        base = self.daily_report_file.read_text(encoding="utf-8") if self.daily_report_file.exists() else "# Brain V8 Daily Report\n"
        add = f"""

## V8 风控与执行状态

```json
{json.dumps({"health": health, "execution_audit": audit, "risk_dashboard": dash}, ensure_ascii=False, indent=2)[:12000]}
```

## V8 检查清单

1. `quality_gate.passed` 没通过时，不要切实盘。
2. `risk_of_ruin` 高时，降低仓位和同时持仓数。
3. 实际滑点高于成本模型时，先调高成本再回测。
4. 同方向/同板块持仓过多时，宁愿不交易。
"""
        self.daily_report_file.write_text(base + add, encoding="utf-8")
        return self.daily_report_file



# =============================================================================
# V9：真实数据准备 + 策略质量门优化层
# =============================================================================

import zipfile
from calendar import monthrange


def _month_iter(start: dt.datetime, end: dt.datetime) -> Iterable[Tuple[int, int]]:
    cur_y, cur_m = start.year, start.month
    end_y, end_m = end.year, end.month
    while (cur_y, cur_m) <= (end_y, end_m):
        yield cur_y, cur_m
        cur_m += 1
        if cur_m > 12:
            cur_y += 1
            cur_m = 1


def _parse_binance_vision_rows(rows: Iterable[Sequence[str]]) -> List[Candle]:
    out: List[Candle] = []
    for r in rows:
        if not r or str(r[0]).lower() in {"open_time", "open time"}:
            continue
        try:
            out.append(Candle(int(float(r[0])), safe_float(r[1]), safe_float(r[2]), safe_float(r[3]), safe_float(r[4]), safe_float(r[5]), int(float(r[6]))))
        except Exception:
            continue
    return out


def _read_klines_from_csv(path: Path) -> List[Candle]:
    candles: List[Candle] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            if "open_time" in sample[:300].lower() or "close_time" in sample[:300].lower():
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        candles.append(Candle(
                            int(float(row.get("open_time", row.get("time", 0)))),
                            safe_float(row.get("open")), safe_float(row.get("high")), safe_float(row.get("low")), safe_float(row.get("close")),
                            safe_float(row.get("volume")), int(float(row.get("close_time", row.get("open_time", row.get("time", 0))))),
                        ))
                    except Exception:
                        continue
            else:
                candles.extend(_parse_binance_vision_rows(csv.reader(f)))
    except Exception as exc:
        logging.warning("读取本地 K线失败 %s: %s", path, exc)
    return candles


def _read_klines_from_zip(path: Path) -> List[Candle]:
    candles: List[Candle] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".csv")]
            for name in names:
                with zf.open(name) as f:
                    text = f.read().decode("utf-8", errors="ignore").splitlines()
                    candles.extend(_parse_binance_vision_rows(csv.reader(text)))
    except Exception as exc:
        logging.warning("读取本地 ZIP K线失败 %s: %s", path, exc)
    return candles


def load_local_klines(data_dir: Path, symbol: str, interval: str, start_ms: Optional[int] = None, end_ms: Optional[int] = None, limit: int = 0) -> List[Candle]:
    if not data_dir or not data_dir.exists():
        return []
    patterns = [
        f"**/{symbol}-{interval}-*.csv",
        f"**/{symbol}-{interval}-*.zip",
        f"**/{symbol}_{interval}.csv",
        f"**/{symbol}-{interval}.csv",
        f"**/{symbol}/{interval}/*.csv",
        f"**/{symbol}/{interval}/*.zip",
    ]
    files: List[Path] = []
    for pat in patterns:
        files.extend(data_dir.glob(pat))
    files = sorted(set(files))
    if not files:
        return []
    rows: List[Candle] = []
    for p in files:
        rows.extend(_read_klines_from_zip(p) if p.suffix.lower() == ".zip" else _read_klines_from_csv(p))
    uniq: Dict[int, Candle] = {}
    for c in rows:
        if start_ms is not None and c.open_time < start_ms:
            continue
        if end_ms is not None and c.open_time > end_ms:
            continue
        if c.open > 0 and c.high >= c.low and c.close > 0:
            uniq[c.open_time] = c
    out = [uniq[k] for k in sorted(uniq)]
    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:]
    return out


class BinanceFuturesClientV9(BinanceFuturesClient):
    """V9 数据客户端：优先读本地真实历史数据缓存；否则走 Binance API 分页。"""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        raw_dir = env_str("LOCAL_KLINE_DIR", env_str("BINANCE_HISTORY_DIR", ""))
        self.local_kline_dir = Path(raw_dir).expanduser() if raw_dir else None
        self.prefer_local = env_bool("PREFER_LOCAL_KLINES", True)
        self.max_historical_klines = env_int("MAX_HISTORICAL_KLINES", 150_000)

    def historical_klines(self, symbol: str, interval: str, start_ms: Optional[int], end_ms: Optional[int], limit: int) -> List[Candle]:
        limit = max(1, min(int(limit), self.max_historical_klines))
        if self.prefer_local and self.local_kline_dir:
            local = load_local_klines(self.local_kline_dir, symbol, interval, start_ms, end_ms, limit)
            if local:
                return local
        if start_ms is None and end_ms is None:
            return self.klines(symbol, interval, min(limit, 1500))
        out: List[Candle] = []
        step = interval_to_ms(interval)
        cursor = start_ms
        if cursor is None and end_ms is not None:
            cursor = max(0, end_ms - step * limit)
        if cursor is None:
            return self.klines(symbol, interval, min(limit, 1500))
        while len(out) < limit:
            batch_limit = min(1500, limit - len(out))
            batch = self.klines(symbol, interval, batch_limit, cursor, end_ms)
            if not batch:
                break
            if out and batch[0].open_time <= out[-1].open_time:
                batch = [c for c in batch if c.open_time > out[-1].open_time]
            out.extend(batch)
            if len(batch) < batch_limit:
                break
            cursor = batch[-1].open_time + step
            if end_ms is not None and cursor >= end_ms:
                break
            time.sleep(env_float("HISTORICAL_PAGE_SLEEP", 0.05))
        return out


def symbol_is_major(symbol: str) -> bool:
    majors = set(parse_csv_symbols(env_str("MAJOR_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT")))
    return symbol.upper() in majors


class StrategyEngineV9(StrategyEngineV7):
    """V9 策略质量门：根据离线短跑结果，默认禁用弱 trend setup，并对山寨币更严格。"""

    def _apply_v9_setup_policy(self, sig: TradeSignal, entry: Sequence[Candle], trend: Sequence[Candle], regime: MarketRegime) -> TradeSignal:
        if sig.entry <= 0:
            return sig
        direction = sig.direction if sig.direction in {"LONG", "SHORT"} else ("LONG" if sig.tp2 > sig.entry else "SHORT")
        setup = sig.setup or "unknown"
        reasons = list(sig.reasons)
        blockers = list(sig.blockers)
        score = safe_float(sig.score, 0.0)
        if setup == "trend" and not env_bool("ENABLE_TREND_SETUP", False):
            blockers.append("V9 默认禁用纯 trend 入场：缺少回踩/突破结构")
        whitelist = {x.strip() for x in env_str("SETUP_WHITELIST", "pullback,breakout").split(",") if x.strip()}
        if whitelist and setup not in whitelist:
            blockers.append(f"setup 不在白名单: {setup}")
        if setup == "breakout":
            if not env_bool("ENABLE_BREAKOUT_SETUP", True):
                blockers.append("breakout setup 已关闭")
            if entry:
                lp = last_wick_profile(entry[-1])
                vol_ma = sma(volumes(entry), 20) or max(entry[-1].volume, 1.0)
                vol_ratio = entry[-1].volume / max(vol_ma, 1e-12)
                min_break_vol = env_float("V9_BREAKOUT_MIN_VOLUME_RATIO", 1.35)
                if vol_ratio < min_break_vol:
                    blockers.append(f"突破量能不足 {vol_ratio:.2f}x < {min_break_vol:.2f}x")
                if direction == "LONG" and lp["close_location"] < env_float("V9_BREAKOUT_LONG_CLOSE_LOC", 0.72):
                    blockers.append(f"多头突破收盘位置不强 {lp['close_location']:.0%}")
                if direction == "SHORT" and lp["close_location"] > env_float("V9_BREAKOUT_SHORT_CLOSE_LOC", 0.28):
                    blockers.append(f"空头突破收盘位置不弱 {lp['close_location']:.0%}")
        if not symbol_is_major(sig.symbol):
            alt_extra_score = env_float("ALT_EXTRA_SCORE", 6.0)
            if score < self.settings.score_threshold + alt_extra_score:
                blockers.append(f"山寨币额外分数门槛不足 {score:.1f} < {self.settings.score_threshold + alt_extra_score:.1f}")
            if sig.atr_pct > env_float("ALT_MAX_ATR_PCT", min(self.settings.max_atr_pct, 4.5)):
                blockers.append(f"山寨币 ATR% 过高 {sig.atr_pct:.2f}%")
            if setup == "breakout" and score < self.settings.score_threshold + env_float("ALT_BREAKOUT_EXTRA_SCORE", 10.0):
                blockers.append("山寨币突破信号需要更高分数")
        edge = self.edge_memory.stats_for(sig.symbol, direction, setup) if hasattr(self, "edge_memory") else None
        if edge:
            n = int(edge.get("trades", 0) or 0)
            exp = safe_float(edge.get("expectancy_R"), 0.0)
            pf = safe_float(edge.get("profit_factor"), 0.0)
            if n >= env_int("V9_EDGE_BLOCK_MIN_TRADES", 20) and (exp < env_float("V9_EDGE_MIN_EXPECTANCY_R", 0.0) or pf < env_float("V9_EDGE_MIN_PF", 1.05)):
                blockers.append(f"EdgeMemory 负/弱期望 n={n} exp={exp:.3f} pf={pf:.2f}")
            elif n >= 8 and exp > 0.08 and pf >= 1.2:
                score += 3
                reasons.append(f"EdgeMemory 支持 setup exp={exp:.2f}R pf={pf:.2f}")
        else:
            if env_bool("V9_UNKNOWN_EDGE_SIZE_DOWN_ONLY", True):
                reasons.append("EdgeMemory 样本不足：允许观察/小仓，不加分")
        sig.direction = direction if not blockers else "NO_TRADE"
        sig.score = clamp(score, 0, 100)
        sig.reasons = list(dict.fromkeys([str(x) for x in reasons if str(x).strip()]))[:18]
        sig.blockers = list(dict.fromkeys([str(x) for x in blockers if str(x).strip()]))[:18]
        return sig

    def analyze_symbol(self, symbol: str, quote_volume: float, regime: MarketRegime) -> TradeSignal:
        if not self.client:
            return self._no_trade(symbol, quote_volume, ["无 client"])
        try:
            trend = self.client.klines(symbol, self.settings.trend_interval, self.settings.kline_limit)
            entry = self.client.klines(symbol, self.settings.entry_interval, self.settings.kline_limit)
            fund = safe_float(self.client.premium_index(symbol).get("lastFundingRate"), 0.0)
            sig = self.analyze_symbol_from_candles(symbol, entry, trend, regime, quote_volume, fund)
            sig = self.adjust_signal_with_institutional_factors(sig, entry, trend, regime)
            sig = self.adjust_signal_v7(sig, entry, trend, regime)
            return self._apply_v9_setup_policy(sig, entry, trend, regime)
        except Exception as exc:
            logging.debug("%s V9 分析失败: %s", symbol, exc)
            return self._no_trade(symbol, quote_volume, [f"分析异常: {exc}"])

    def analyze_symbol_from_candles(self, symbol: str, entry: Sequence[Candle], trend: Sequence[Candle], regime: MarketRegime, quote_volume: float = 0.0, funding_rate: float = 0.0) -> TradeSignal:
        sig = StrategyEngine.analyze_symbol_from_candles(self, symbol, entry, trend, regime, quote_volume, funding_rate)
        sig = self.adjust_signal_with_institutional_factors(sig, entry, trend, regime)
        sig = self.adjust_signal_v7(sig, entry, trend, regime)
        return self._apply_v9_setup_policy(sig, entry, trend, regime)


class RiskManagerV9(RiskManagerV7):
    def build_plan(self, signal: TradeSignal, equity: float, open_positions: Dict[str, Any], total_notional: float, cooldowns: Dict[str, str]) -> Tuple[Optional[OrderPlan], List[str]]:
        plan, blockers = super().build_plan(signal, equity, open_positions, total_notional, cooldowns)
        if not plan:
            return plan, blockers
        scale = 1.0
        if signal.setup == "breakout":
            scale *= env_float("BREAKOUT_SIZE_SCALE", 0.70)
        if not symbol_is_major(signal.symbol):
            scale *= env_float("ALT_SIZE_SCALE", 0.65)
        if signal.setup == "trend":
            scale *= env_float("TREND_SIZE_SCALE", 0.35)
        if scale >= 0.999:
            return plan, blockers
        rule = self.rules.get(plan.symbol)
        if not rule:
            return plan, blockers
        new_qty = rule.qty_float(plan.qty * clamp(scale, 0.1, 1.0))
        new_notional = new_qty * plan.entry
        new_risk = plan.risk_usdt * (new_qty / max(plan.qty, 1e-12))
        if new_qty <= 0 or (rule.min_qty and new_qty < rule.min_qty) or (rule.min_notional and new_notional < rule.min_notional):
            return None, ["V9 setup/山寨币缩仓后低于最小下单要求"]
        plan.qty = new_qty
        plan.notional = new_notional
        plan.risk_usdt = new_risk
        plan.reasons = list(plan.reasons) + [f"V9 setup/品种缩仓 scale={scale:.2f}"]
        return plan, []


class BrainV9(BrainV8):
    def __init__(self, settings: Settings):
        # V9 不调用父类 __init__，避免先创建 V8 client 导致本地历史缓存/长分页不生效。
        self.settings = settings
        setup_logging(settings)
        self.client = BinanceFuturesClientV9(settings)
        self.journal = TradeJournal(settings.journal_file)
        self.rules = self.client.symbol_rules()
        self.scanner = MarketScannerV7(settings, self.client)
        self.strategy = StrategyEngineV9(settings, self.client)
        self.risk = RiskManagerV9(settings, self.rules)
        self.backtester = BacktesterV8(settings, self.client)
        self.broker: Any = PaperBroker(settings, self.journal) if settings.trading_mode == "paper" else ExchangeBrokerV6(settings, self.client, self.journal, self.rules)
        self.ai_layer = AiDecisionLayer(settings)
        self.governor = RiskGovernorV8(settings)
        self.ai_candidates_file = settings.workspace / "ai_candidates.json"
        self.ai_prompt_file = settings.workspace / "aipro_prompt.md"
        self.ai_decisions_log = settings.workspace / "ai_decisions.jsonl"
        self.process_file = settings.workspace / "process_dashboard.json"
        self.daily_report_file = settings.workspace / "daily_report.md"
        self.circuit = SystemCircuitBreaker(settings)
        self.health = HealthMonitor(settings, self.client, self.broker)
        self.auditor = ExecutionAuditor(settings, self.broker)
        self.portfolio_risk = PortfolioRiskEngine(settings)
        self.v8_dashboard_file = settings.workspace / "v8_risk_dashboard.json"
        self.cost_report_file = settings.workspace / "cost_model_report.json"
        self.parameter_report_file = settings.workspace / "parameter_governance.json"
        self.v9_report_file = settings.workspace / "v9_strategy_quality_report.json"

    def build_ai_payload(self, regime: MarketRegime, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> Dict[str, Any]:
        payload = super().build_ai_payload(regime, signals, marks)
        payload["schema"] = "brain_v9_ai_candidates_v1"
        if "process" in payload:
            payload["process"]["version"] = "V9"
            payload["process"]["new_gates"] = [
                "trend_setup_disabled_by_default",
                "altcoin_extra_score_gate",
                "breakout_close_location_and_volume_gate",
                "edge_memory_negative_expectancy_block",
                "local_real_history_cache_supported",
            ]
        for row in payload.get("candidates", []):
            if row.get("setup") == "trend" and not env_bool("ENABLE_TREND_SETUP", False):
                row["live_ready"] = False
                row.setdefault("risk_blockers", []).append("V9 live 禁用纯 trend setup")
            if not symbol_is_major(str(row.get("symbol", ""))):
                row["ai_priority"] = safe_float(row.get("ai_priority"), 0.0) - env_float("ALT_AI_PRIORITY_PENALTY", 4.0)
        _atomic_write_json(self.ai_candidates_file, payload)
        self._write_aipro_prompt(payload)
        self._write_process_dashboard(payload)
        self.write_v9_strategy_quality_report(payload)
        return payload

    def write_v9_strategy_quality_report(self, payload: Dict[str, Any]) -> Path:
        rows = payload.get("candidates", []) if isinstance(payload, dict) else []
        by_setup: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            s = str(r.get("setup", "unknown"))
            d = by_setup.setdefault(s, {"count": 0, "approved": 0, "live_ready": 0, "avg_score": 0.0})
            d["count"] += 1
            d["approved"] += 1 if r.get("risk_approved") else 0
            d["live_ready"] += 1 if r.get("live_ready") else 0
            d["avg_score"] += safe_float(r.get("score"), 0.0)
        for d in by_setup.values():
            if d["count"]:
                d["avg_score"] = round(d["avg_score"] / d["count"], 2)
        obj = {
            "time": utc_now().isoformat(),
            "version": "V9",
            "policy": {
                "ENABLE_TREND_SETUP": env_bool("ENABLE_TREND_SETUP", False),
                "SETUP_WHITELIST": env_str("SETUP_WHITELIST", "pullback,breakout"),
                "ALT_EXTRA_SCORE": env_float("ALT_EXTRA_SCORE", 6.0),
                "ALT_SIZE_SCALE": env_float("ALT_SIZE_SCALE", 0.65),
                "BREAKOUT_SIZE_SCALE": env_float("BREAKOUT_SIZE_SCALE", 0.70),
            },
            "by_setup": by_setup,
            "notes": [
                "V9 默认关闭纯 trend 入场，因为离线短跑中 trend setup 明显拖累结果。",
                "山寨币需要更高分数、较低波动和更小仓位；BTC/ETH/BNB/SOL 作为 major 可相对宽松。",
                "真实效果必须用 Binance 真实历史 K线验证，本报告只说明当前候选质量门。",
            ],
        }
        _atomic_write_json(self.v9_report_file, obj)
        return self.v9_report_file

    def _write_aipro_prompt(self, payload: Dict[str, Any]) -> None:
        super()._write_aipro_prompt(payload)
        extra = """

## V9 额外规则

- 默认不要选择 `setup=trend` 的候选，除非配置明确启用且候选为 A 级。
- 对山寨币候选要更保守：同等分数下优先 BTC/ETH/高流动性 major。
- 突破类候选必须同时满足量能、收盘位置、盘口成本和 EdgeMemory 条件。
- 如果候选显示 EdgeMemory 负期望或样本不足，不允许因为新闻利好单独交易。
"""
        with self.ai_prompt_file.open("a", encoding="utf-8") as f:
            f.write(extra)



# =============================================================================
# V10：Ai Pro 原生运行优化 / 策略质量门升级
# =============================================================================


def _v10_percentile(values: Sequence[float], q: float, default: float = 0.0) -> float:
    vals = sorted([float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))])
    if not vals:
        return default
    if len(vals) == 1:
        return vals[0]
    pos = clamp(q, 0.0, 1.0) * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - pos) + vals[hi] * (pos - lo)


def _v10_close_location(c: Candle) -> float:
    rng = max(c.high - c.low, 1e-12)
    return clamp((c.close - c.low) / rng, 0.0, 1.0)


def _v10_median(values: Sequence[float], default: float = 0.0) -> float:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    return statistics.median(vals) if vals else default


class StrategyEngineV10(StrategyEngineV9):
    """V10 目标：减少“看起来有信号但统计质量差”的交易。"""

    def _apply_v10_quality_policy(self, sig: TradeSignal, entry: Sequence[Candle], trend: Sequence[Candle], regime: MarketRegime) -> TradeSignal:
        if sig.direction not in {"LONG", "SHORT"}:
            return sig
        reasons = list(sig.reasons)
        blockers = list(sig.blockers)
        score = float(sig.score)
        direction = sig.direction
        setup = (sig.setup or "unknown").lower()
        closes = [c.close for c in entry]
        vols = [c.volume for c in entry]
        if len(entry) < 90 or len(trend) < 80:
            blockers.append("V10 数据不足：entry/trend K线数量不够")
        last = entry[-1]
        prev = entry[-2] if len(entry) >= 2 else last
        atr_v = atr(entry[-60:], 14) or sig.atr or 0.0
        if atr_v <= 0:
            blockers.append("V10 ATR 无效")
            atr_v = max(sig.entry * 0.003, 1e-9)

        if regime.bias == "CHAOS":
            blockers.append("V10 大盘 CHAOS：停止新仓")
        if env_bool("V10_STRICT_MARKET_ALIGNMENT", True) and regime.bias in {"LONG", "SHORT"} and direction != regime.bias:
            edge = self.edge_memory.stats_for(sig.symbol, direction, setup) if hasattr(self, "edge_memory") else None
            exp = safe_float(edge.get("expectancy_R"), 0.0) if edge else 0.0
            min_counter_score = env_float("V10_COUNTER_MARKET_MIN_SCORE", self.settings.score_threshold + 12.0)
            if score < min_counter_score or exp < env_float("V10_COUNTER_MARKET_MIN_EDGE", 0.12):
                blockers.append(f"V10 逆大盘信号不足 score={score:.1f} edge={exp:.2f}")
            else:
                reasons.append("逆大盘但分数/Edge 足够，允许小仓观察")
                score -= env_float("V10_COUNTER_MARKET_SCORE_PENALTY", 4.0)

        recent_ranges = [(c.high - c.low) / max(c.close, 1e-12) * 100.0 for c in entry[-80:]]
        p90_range = _v10_percentile(recent_ranges, 0.90, sig.atr_pct)
        last_range_pct = (last.high - last.low) / max(last.close, 1e-12) * 100.0
        if last_range_pct > max(p90_range * env_float("V10_MAX_LAST_RANGE_P90_MULT", 1.35), env_float("V10_MAX_SIGNAL_CANDLE_RANGE_PCT", 3.8)):
            blockers.append(f"V10 信号K线过大，疑似追单/插针 range={last_range_pct:.2f}% p90={p90_range:.2f}%")

        ema20 = ema(closes[-80:], 20)
        ema60 = ema(closes[-100:], 60)
        if ema20:
            extension_atr = abs(last.close - ema20) / max(atr_v, 1e-12)
            if extension_atr > env_float("V10_MAX_EXTENSION_ATR", 2.8):
                blockers.append(f"V10 离 EMA20 过远，不追单 extension={extension_atr:.2f} ATR")
            elif extension_atr < env_float("V10_HEALTHY_EXTENSION_ATR", 1.4):
                score += 1.5
                reasons.append("V10 入场未明显追高/追空")

        if setup == "pullback":
            if not ema20 or not ema60:
                blockers.append("V10 pullback 无 EMA 数据")
            else:
                recent = entry[-8:]
                if direction == "LONG":
                    touched = any(c.low <= ema20 + atr_v * env_float("V10_PULLBACK_TOUCH_ATR", 0.28) for c in recent[:-1])
                    reclaimed = last.close > ema20 and last.close > prev.close and _v10_close_location(last) >= env_float("V10_PULLBACK_LONG_CLOSE_LOC", 0.56)
                    trend_ok = ema20 >= ema60
                    if not touched:
                        blockers.append("V10 多头回踩不充分")
                    if not reclaimed:
                        blockers.append("V10 多头回踩后未重新转强")
                    if not trend_ok:
                        blockers.append("V10 多头回踩与 EMA 趋势不一致")
                else:
                    touched = any(c.high >= ema20 - atr_v * env_float("V10_PULLBACK_TOUCH_ATR", 0.28) for c in recent[:-1])
                    reclaimed = last.close < ema20 and last.close < prev.close and _v10_close_location(last) <= env_float("V10_PULLBACK_SHORT_CLOSE_LOC", 0.44)
                    trend_ok = ema20 <= ema60
                    if not touched:
                        blockers.append("V10 空头反弹不充分")
                    if not reclaimed:
                        blockers.append("V10 空头反弹后未重新转弱")
                    if not trend_ok:
                        blockers.append("V10 空头回踩与 EMA 趋势不一致")
                if not any(b.startswith("V10 多头回踩") or b.startswith("V10 空头") or b == "V10 pullback 无 EMA 数据" for b in blockers):
                    score += env_float("V10_PULLBACK_QUALITY_BONUS", 3.0)
                    reasons.append("V10 pullback 质量门通过")

        if setup == "breakout":
            lookback = env_int("V10_BREAKOUT_LOOKBACK", 28)
            if len(entry) >= lookback + 20:
                base = entry[-lookback-1:-1]
                prior_hi = max(c.high for c in base)
                prior_lo = min(c.low for c in base)
                base_range_pct = (prior_hi - prior_lo) / max(last.close, 1e-12) * 100.0
                med_range_pct = _v10_median(recent_ranges[-80:], sig.atr_pct)
                compressed = base_range_pct <= med_range_pct * env_float("V10_BREAKOUT_COMPRESSION_MULT", 2.8)
                vol_med = _v10_median(vols[-40:-1], 0.0)
                vol_ok = vol_med > 0 and last.volume >= vol_med * env_float("V10_BREAKOUT_MIN_VOL_MED", 1.25)
                if direction == "LONG":
                    broke = last.close > prior_hi and _v10_close_location(last) >= env_float("V10_BREAKOUT_LONG_CLOSE_LOC", 0.76)
                else:
                    broke = last.close < prior_lo and _v10_close_location(last) <= env_float("V10_BREAKOUT_SHORT_CLOSE_LOC", 0.24)
                if not compressed:
                    blockers.append("V10 突破前未形成足够压缩结构")
                if not vol_ok:
                    blockers.append("V10 突破量能未超过中位量能")
                if not broke:
                    blockers.append("V10 突破收盘确认不足")
                if compressed and vol_ok and broke:
                    score += env_float("V10_BREAKOUT_QUALITY_BONUS", 4.0)
                    reasons.append("V10 breakout 压缩→放量→收盘确认通过")

        if not symbol_is_major(sig.symbol):
            alt_gate = env_float("V10_ALT_EXTRA_SCORE", env_float("ALT_EXTRA_SCORE", 6.0) + 2.0)
            if score < self.settings.score_threshold + alt_gate:
                blockers.append(f"V10 山寨币总分不足 {score:.1f} < {self.settings.score_threshold + alt_gate:.1f}")
            if setup == "breakout" and not env_bool("V10_ALLOW_ALT_BREAKOUT", False):
                blockers.append("V10 默认禁止山寨币突破追单")
            score -= env_float("V10_ALT_SCORE_PENALTY", 1.5)

        edge = self.edge_memory.stats_for(sig.symbol, direction, setup) if hasattr(self, "edge_memory") else None
        if edge:
            n = int(edge.get("trades", 0) or 0)
            exp = safe_float(edge.get("expectancy_R"), 0.0)
            pf = safe_float(edge.get("profit_factor"), 0.0)
            if n >= env_int("V10_EDGE_STRICT_MIN_TRADES", 30):
                if exp < env_float("V10_EDGE_REQUIRED_EXPECTANCY_R", 0.03) or pf < env_float("V10_EDGE_REQUIRED_PF", 1.12):
                    blockers.append(f"V10 EdgeMemory 不达标 n={n} exp={exp:.3f} pf={pf:.2f}")
                else:
                    score += clamp(exp * 10.0, 0.0, 4.0)
                    reasons.append(f"V10 EdgeMemory 达标 n={n} exp={exp:.2f}R pf={pf:.2f}")
            elif env_bool("V10_REQUIRE_EDGE_FOR_LIVE_READY", True):
                reasons.append(f"V10 EdgeMemory 样本不足 n={n}：paper可观察，live降级")
        else:
            reasons.append("V10 无 EdgeMemory：paper可观察，live需谨慎")

        sig.score = clamp(score, 0, 100)
        sig.direction = direction if not blockers and sig.score >= self.settings.score_threshold else "NO_TRADE"
        if sig.score < self.settings.score_threshold:
            blockers.append(f"V10 分数低于门槛 {sig.score:.1f} < {self.settings.score_threshold:.1f}")
        sig.reasons = list(dict.fromkeys([str(x) for x in reasons if str(x).strip()]))[:22]
        sig.blockers = list(dict.fromkeys([str(x) for x in blockers if str(x).strip()]))[:22]
        return sig

    def analyze_symbol(self, symbol: str, quote_volume: float, regime: MarketRegime) -> TradeSignal:
        if not self.client:
            return self._no_trade(symbol, quote_volume, ["无 client"])
        try:
            trend = self.client.klines(symbol, self.settings.trend_interval, self.settings.kline_limit)
            entry = self.client.klines(symbol, self.settings.entry_interval, self.settings.kline_limit)
            fund = safe_float(self.client.premium_index(symbol).get("lastFundingRate"), 0.0)
            return self.analyze_symbol_from_candles(symbol, entry, trend, regime, quote_volume, fund)
        except Exception as exc:
            logging.debug("%s V10 分析失败: %s", symbol, exc)
            return self._no_trade(symbol, quote_volume, [f"分析异常: {exc}"])

    def analyze_symbol_from_candles(self, symbol: str, entry: Sequence[Candle], trend: Sequence[Candle], regime: MarketRegime, quote_volume: float = 0.0, funding_rate: float = 0.0) -> TradeSignal:
        sig = StrategyEngineV9.analyze_symbol_from_candles(self, symbol, entry, trend, regime, quote_volume, funding_rate)
        return self._apply_v10_quality_policy(sig, entry, trend, regime)


class RiskManagerV10(RiskManagerV9):
    def build_plan(self, signal: TradeSignal, equity: float, open_positions: Dict[str, Any], total_notional: float, cooldowns: Dict[str, str]) -> Tuple[Optional[OrderPlan], List[str]]:
        plan, blockers = super().build_plan(signal, equity, open_positions, total_notional, cooldowns)
        if not plan:
            return plan, blockers
        scale = 1.0
        score_margin = max(0.0, signal.score - self.settings.score_threshold)
        if score_margin < env_float("V10_FULL_SIZE_SCORE_MARGIN", 10.0):
            scale *= env_float("V10_LOW_MARGIN_SIZE_SCALE", 0.72)
        edge = EdgeMemory(self.settings.workspace / "edge_memory.json").stats_for(signal.symbol, signal.direction, signal.setup)
        n = int(edge.get("trades", 0) or 0) if edge else 0
        exp = safe_float(edge.get("expectancy_R"), 0.0) if edge else 0.0
        if n < env_int("V10_EDGE_NORMAL_SIZE_MIN_TRADES", 30) or exp < env_float("V10_EDGE_NORMAL_SIZE_MIN_EXP", 0.06):
            scale *= env_float("V10_UNKNOWN_EDGE_SIZE_SCALE", 0.65)
        if self.settings.trading_mode == "live":
            scale *= env_float("V10_LIVE_INITIAL_SIZE_SCALE", 0.50)
        if signal.atr_pct > env_float("V10_HIGH_VOL_SIZE_ATR_PCT", 3.5):
            scale *= env_float("V10_HIGH_VOL_SIZE_SCALE", 0.60)
        rule = self.rules.get(plan.symbol)
        if not rule:
            return None, ["V10 缺少交易规则"]
        scale = clamp(scale, env_float("V10_MIN_SIZE_SCALE", 0.20), 1.0)
        new_qty = rule.qty_float(plan.qty * scale)
        new_notional = new_qty * plan.entry
        new_risk = plan.risk_usdt * (new_qty / max(plan.qty, 1e-12))
        if new_qty <= 0 or (rule.min_qty and new_qty < rule.min_qty) or (rule.min_notional and new_notional < rule.min_notional):
            return None, ["V10 质量缩仓后低于最小下单要求"]
        plan.qty = new_qty
        plan.notional = new_notional
        plan.risk_usdt = new_risk
        plan.reasons = list(plan.reasons) + [f"V10 质量/Edge/实盘缩仓 scale={scale:.2f}"]
        return plan, []


class RiskGovernorV10(RiskGovernorV7):
    def approve(self, decision: AIDecision, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        candidate, notes = super().approve(decision, payload)
        if not candidate:
            return candidate, notes
        blockers = list(notes)
        setup = str(candidate.get("setup", "")).lower()
        tier = str(candidate.get("process_tier", "")).upper()
        score = safe_float(candidate.get("score"), 0.0)
        live_ready = bool(candidate.get("live_ready"))
        whitelist = [s.lower() for s in parse_csv_symbols(env_str("V10_LIVE_SETUP_WHITELIST", "PULLBACK,BREAKOUT"))]
        if whitelist and setup not in whitelist:
            blockers.append(f"V10 setup 不在白名单: {setup}")
        if tier not in {"A", "B"}:
            blockers.append(f"V10 只允许 A/B 候选进入执行，当前 tier={tier}")
        if score < env_float("V10_AI_EXEC_MIN_SCORE", 82.0):
            blockers.append(f"V10 AI 执行分数不足 {score:.1f}")
        if self.settings.trading_mode == "live" and not live_ready:
            blockers.append("V10 live_ready=false，禁止实盘")
        if candidate.get("risk_blockers"):
            blockers.append("V10 候选仍包含 risk_blockers")
        if blockers:
            return None, blockers
        return candidate, ["V10 RiskGovernor 最终通过"]


class BrainV10(BrainV9):
    def __init__(self, settings: Settings):
        self.settings = settings
        setup_logging(settings)
        self.client = BinanceFuturesClientV9(settings)
        self.journal = TradeJournal(settings.journal_file)
        self.rules = self.client.symbol_rules()
        self.scanner = MarketScannerV7(settings, self.client)
        self.strategy = StrategyEngineV10(settings, self.client)
        self.risk = RiskManagerV10(settings, self.rules)
        self.backtester = BacktesterV8(settings, self.client)
        self.broker: Any = PaperBroker(settings, self.journal) if settings.trading_mode == "paper" else ExchangeBrokerV6(settings, self.client, self.journal, self.rules)
        self.ai_layer = AiDecisionLayer(settings)
        self.governor = RiskGovernorV10(settings)
        self.ai_candidates_file = settings.workspace / "ai_candidates.json"
        self.ai_prompt_file = settings.workspace / "aipro_prompt.md"
        self.ai_decisions_log = settings.workspace / "ai_decisions.jsonl"
        self.process_file = settings.workspace / "process_dashboard.json"
        self.daily_report_file = settings.workspace / "daily_report.md"
        self.circuit = SystemCircuitBreaker(settings)
        self.health = HealthMonitor(settings, self.client, self.broker)
        self.auditor = ExecutionAuditor(settings, self.broker)
        self.portfolio_risk = PortfolioRiskEngine(settings)
        self.v8_dashboard_file = settings.workspace / "v10_risk_dashboard.json"
        self.cost_report_file = settings.workspace / "cost_model_report.json"
        self.parameter_report_file = settings.workspace / "parameter_governance.json"
        self.v9_report_file = settings.workspace / "v9_strategy_quality_report.json"
        self.v10_report_file = settings.workspace / "v10_aipro_strategy_report.json"
        self.aipro_deploy_prompt_file = settings.workspace / "aipro_deployment_prompt.md"

    def build_ai_payload(self, regime: MarketRegime, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> Dict[str, Any]:
        payload = super().build_ai_payload(regime, signals, marks)
        payload["schema"] = "brain_v10_aipro_candidates_v1"
        payload["aipro_runtime_contract"] = {
            "mode_default": self.settings.trading_mode,
            "ai_role": "rank_only_from_candidates",
            "cannot_modify": ["entry", "stop_loss", "tp1", "tp2", "qty", "risk_usdt", "leverage"],
            "must_respect": ["risk_approved", "live_ready", "process_tier", "risk_blockers", "edge_memory"],
            "safe_action": "NO_TRADE",
        }
        payload.setdefault("process", {})["version"] = "V10"
        payload["process"]["strategy_policy"] = [
            "pullback 优先；breakout 只做压缩后放量确认；trend 默认禁用",
            "逆大盘需要高分和正 Edge；山寨币需要更高分且默认缩仓",
            "AI/Ai Pro 只做候选排序，不允许放宽风控",
        ]
        for row in payload.get("candidates", []):
            row["v10_quality_flags"] = {
                "setup_allowed": str(row.get("setup", "")).lower() in {"pullback", "breakout"},
                "tier_ok": str(row.get("process_tier", "")).upper() in {"A", "B"},
                "score_ok": safe_float(row.get("score"), 0.0) >= env_float("V10_AI_EXEC_MIN_SCORE", 82.0),
                "risk_clean": not bool(row.get("risk_blockers")),
                "live_ready": bool(row.get("live_ready")),
            }
        _atomic_write_json(self.ai_candidates_file, payload)
        self._write_aipro_prompt(payload)
        self._write_process_dashboard(payload)
        self.write_v10_aipro_strategy_report(payload)
        self.write_aipro_deployment_prompt()
        return payload

    def _write_aipro_prompt(self, payload: Dict[str, Any]) -> None:
        super()._write_aipro_prompt(payload)
        extra = """

## V10 / Ai Pro 直接运行规则

你是交易执行代理，不是自由预测模型。你只能从 `ai_candidates.json` 的候选中选择，不能自己发明交易。

强制规则：
1. 只允许选择 `risk_approved=true`、`live_ready=true`、`process_tier=A/B`、且 `risk_blockers=[]` 的候选。
2. 只允许 `setup=pullback` 或通过 V10 压缩/放量/收盘确认的 `setup=breakout`。
3. 不允许修改 entry、stop_loss、tp1、tp2、qty、risk_usdt、leverage。
4. 若没有 A/B 且 live_ready 的候选，输出 `{\"decision\":\"NO_TRADE\"}`。
5. 实盘 live 时优先不交易；只有候选质量极高且风控通过才允许选择 1 笔。
6. 新闻利好不能单独作为开仓原因；负面新闻或风险事件优先否决。
7. 任何不确定、API异常、数据不足、风控冲突，默认 `NO_TRADE`。

输出 JSON，只能是：
```json
{
  "decision": "TRADE 或 NO_TRADE",
  "symbol": "候选里的 symbol",
  "direction": "LONG 或 SHORT",
  "confidence": 0.0,
  "reason": ["最多5条"],
  "risk_notes": ["最多5条"]
}
```
"""
        with self.ai_prompt_file.open("a", encoding="utf-8") as f:
            f.write(extra)

    def write_v10_aipro_strategy_report(self, payload: Dict[str, Any]) -> Path:
        rows = payload.get("candidates", []) if isinstance(payload, dict) else []
        tier_count: Dict[str, int] = {}
        setup_count: Dict[str, int] = {}
        live_ready = 0
        clean = 0
        for r in rows:
            tier_count[str(r.get("process_tier", "unknown"))] = tier_count.get(str(r.get("process_tier", "unknown")), 0) + 1
            setup_count[str(r.get("setup", "unknown"))] = setup_count.get(str(r.get("setup", "unknown")), 0) + 1
            live_ready += 1 if r.get("live_ready") else 0
            clean += 1 if not r.get("risk_blockers") else 0
        obj = {
            "time": utc_now().isoformat(),
            "version": "V10",
            "purpose": "Ai Pro 原生运行：AI 只能排序候选，RiskGovernor 最终否决。",
            "summary": {"candidates": len(rows), "live_ready": live_ready, "risk_clean": clean, "tier_count": tier_count, "setup_count": setup_count},
            "policy": {
                "V10_STRICT_MARKET_ALIGNMENT": env_bool("V10_STRICT_MARKET_ALIGNMENT", True),
                "V10_AI_EXEC_MIN_SCORE": env_float("V10_AI_EXEC_MIN_SCORE", 82.0),
                "V10_ALLOW_ALT_BREAKOUT": env_bool("V10_ALLOW_ALT_BREAKOUT", False),
                "V10_REQUIRE_EDGE_FOR_LIVE_READY": env_bool("V10_REQUIRE_EDGE_FOR_LIVE_READY", True),
                "V10_LIVE_INITIAL_SIZE_SCALE": env_float("V10_LIVE_INITIAL_SIZE_SCALE", 0.50),
            },
            "notes": [
                "V10 是策略质量门升级，不保证盈利；真实上线前仍需 paper/demo/小资金验证。",
                "默认策略偏保守：pullback > breakout > trend，山寨币额外严格。",
                "Ai Pro 若无法确认候选数据来源或风控状态，应输出 NO_TRADE。",
            ],
        }
        _atomic_write_json(self.v10_report_file, obj)
        return self.v10_report_file

    def write_aipro_deployment_prompt(self) -> Path:
        text = """# Brain V10 / Binance Ai Pro 部署提示词

你将在 Binance Ai Pro 的隔离 AI 子账户环境中运行一个合约量化交易代理。默认先使用 paper 或 demo，不要直接 live。

## 运行目标
1. 使用 `brain_v10_aipro.py` 扫描 USDⓈ-M 永续合约。
2. 生成 `ai_candidates.json` 与 `aipro_prompt.md`。
3. 只从风控通过的候选中选择最多 1 笔交易。
4. 若没有高质量候选，输出 NO_TRADE。

## 最小安全配置
```bash
export TRADING_MODE=paper
export AI_DECISION_MODE=rule
export BRAIN_WS=./brain_v10_data
export SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
export SCORE_THRESHOLD=80
export V10_AI_EXEC_MIN_SCORE=82
export RISK_PER_TRADE=0.003
export MAX_POSITIONS=1
export MAX_NEW_ENTRIES_PER_CYCLE=1
```

## Demo/实盘切换
- demo：需要 demo key/secret，并设置 `TRADING_MODE=demo`。
- live：必须额外设置 `LIVE_TRADING_CONFIRM=YES`、`USE_EXCHANGE_PROTECTION=1`，并建议 `LIVE_ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT`、`LIVE_MAX_ORDER_NOTIONAL=50`。

## 决策原则
- AI 只能排序候选，不能修改止损、止盈、仓位。
- RiskGovernor 拥有最终否决权。
- API 异常、数据不足、新闻负面、EdgeMemory 负期望、大盘 CHAOS，一律 NO_TRADE。
- 实盘初期只允许 A 级候选，小仓位。

## 建议命令
```bash
python3 brain_v10_aipro.py --scan
python3 brain_v10_aipro.py --healthcheck
python3 brain_v10_aipro.py --once
```
"""
        self.aipro_deploy_prompt_file.write_text(text, encoding="utf-8")
        return self.aipro_deploy_prompt_file

def download_binance_vision_monthly(symbols: Sequence[str], interval: str, start: str, end: str, dest: Path, market: str = "um") -> Dict[str, Any]:
    start_dt = dt.datetime.fromisoformat(start.replace("Z", "+00:00")) if "T" in start else dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    end_dt = dt.datetime.fromisoformat(end.replace("Z", "+00:00")) if "T" in end else dt.datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)
    dest.mkdir(parents=True, exist_ok=True)
    result = {"downloaded": [], "skipped": [], "failed": []}
    base = env_str("BINANCE_VISION_BASE", "https://data.binance.vision/data/futures")
    session = requests.Session()
    for sym in symbols:
        for y, m in _month_iter(start_dt, end_dt):
            name = f"{sym}-{interval}-{y:04d}-{m:02d}.zip"
            url = f"{base}/{market}/monthly/klines/{sym}/{interval}/{name}"
            out = dest / "futures" / market / "monthly" / "klines" / sym / interval / name
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists() and out.stat().st_size > 0:
                result["skipped"].append(str(out))
                continue
            try:
                r = session.get(url, timeout=env_int("DOWNLOAD_TIMEOUT", 30))
                if r.status_code == 404:
                    result["failed"].append({"url": url, "error": "404 not found"})
                    continue
                r.raise_for_status()
                tmp = out.with_suffix(out.suffix + ".tmp")
                tmp.write_bytes(r.content)
                os.replace(tmp, out)
                result["downloaded"].append(str(out))
                time.sleep(env_float("DOWNLOAD_SLEEP", 0.1))
            except Exception as exc:
                result["failed"].append({"url": url, "error": str(exc)})
    manifest = {"time": utc_now().isoformat(), "symbols": list(symbols), "interval": interval, "start": start, "end": end, "dest": str(dest), **result}
    _atomic_write_json(dest / "download_manifest.json", manifest)
    return manifest


# =============================================================================
# V11：Institutional AI Quant Final OS / 最终模拟盘候选版本
# =============================================================================


def _v11_load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logging.exception("V11 读取 JSON 失败: %s", path)
    return default


def _v11_truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "ok", "pass"}
    return bool(value)


def _v11_metric(metrics: Dict[str, Any], *names: str, default: float = 0.0) -> float:
    for n in names:
        if n in metrics:
            return safe_float(metrics.get(n), default)
    return default


def _v11_assess_research_quality(settings: Settings) -> Dict[str, Any]:
    """读取本地回测/压力测试/参数治理输出，生成上线前研究质量门。不会联网。"""
    metrics = _v11_load_json(settings.metrics_file, {})
    stress = _v11_load_json(settings.workspace / "stress_test.json", {})
    governance = _v11_load_json(settings.workspace / "parameter_governance.json", {})
    trades = int(_v11_metric(metrics, "total_trades", "trades", default=0))
    expectancy = _v11_metric(metrics, "expectancy_R", "expectancy", default=0.0)
    pf = _v11_metric(metrics, "profit_factor", default=0.0)
    mdd = abs(_v11_metric(metrics, "max_drawdown_R", "max_drawdown", default=999.0))
    risk_ruin = _v11_metric(stress, "risk_of_ruin", default=0.0)
    p05 = _v11_metric(stress, "p05_total_R", default=0.0)

    min_trades = env_int("V11_MIN_RESEARCH_TRADES", 120)
    min_exp = env_float("V11_MIN_EXPECTANCY_R", 0.03)
    min_pf = env_float("V11_MIN_PROFIT_FACTOR", 1.12)
    max_mdd = env_float("V11_MAX_DRAWDOWN_R", 25.0)
    max_ruin = env_float("V11_MAX_RISK_OF_RUIN", 0.05)
    min_p05 = env_float("V11_MIN_STRESS_P05_TOTAL_R", -15.0)

    checks = {
        "metrics_file_exists": settings.metrics_file.exists(),
        "stress_file_exists": (settings.workspace / "stress_test.json").exists(),
        "min_trades": trades >= min_trades,
        "positive_expectancy": expectancy >= min_exp,
        "profit_factor": pf >= min_pf,
        "drawdown_limit": mdd <= max_mdd,
        "risk_of_ruin_limit": risk_ruin <= max_ruin,
        "stress_tail_limit": p05 >= min_p05,
    }
    pass_count = sum(1 for v in checks.values() if v)
    quality_score = round(pass_count / max(len(checks), 1) * 100.0, 2)
    passed = all(checks.values())
    return {
        "time": utc_now().isoformat(),
        "passed": passed,
        "quality_score": quality_score,
        "checks": checks,
        "thresholds": {
            "V11_MIN_RESEARCH_TRADES": min_trades,
            "V11_MIN_EXPECTANCY_R": min_exp,
            "V11_MIN_PROFIT_FACTOR": min_pf,
            "V11_MAX_DRAWDOWN_R": max_mdd,
            "V11_MAX_RISK_OF_RUIN": max_ruin,
            "V11_MIN_STRESS_P05_TOTAL_R": min_p05,
        },
        "observed": {
            "trades": trades,
            "expectancy_R": expectancy,
            "profit_factor": pf,
            "max_drawdown_R": mdd,
            "risk_of_ruin": risk_ruin,
            "p05_total_R": p05,
        },
        "governance": governance,
    }


def _v11_live_allowed(symbol: str) -> bool:
    allowed = parse_csv_symbols(env_str("LIVE_ALLOWED_SYMBOLS", ""))
    if allowed and symbol.upper() not in allowed:
        return False
    if env_bool("V11_LIVE_MAJOR_ONLY", True) and not symbol_is_major(symbol):
        return False
    return True


def _v11_candidate_gate(row: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """对 AI 候选做最终机构化质量门打标。只打标，不替代 RiskGovernor。"""
    symbol = str(row.get("symbol", "")).upper()
    setup = str(row.get("setup", "")).lower()
    tier = str(row.get("process_tier", "")).upper()
    score = safe_float(row.get("score"), 0.0)
    rr = safe_float(row.get("rr"), 0.0)
    edge_n = int(row.get("edge_trades", 0) or 0)
    edge_exp = safe_float(row.get("edge_expectancy_R"), 0.0)
    edge_pf = safe_float(row.get("edge_profit_factor"), 0.0)
    blockers: List[str] = []
    warnings: List[str] = []

    allowed_setups = {s.strip().lower() for s in env_str("V11_SETUP_WHITELIST", "pullback,breakout").split(",") if s.strip()}
    if setup not in allowed_setups:
        blockers.append(f"V11 setup 不在白名单: {setup}")
    if tier and tier not in {"A", "B"}:
        blockers.append(f"V11 候选等级不足: {tier}")
    if not _v11_truth(row.get("risk_approved")):
        blockers.append("V11 risk_approved=false")
    if row.get("risk_blockers"):
        blockers.append("V11 候选仍有 risk_blockers")
    if score < env_float("V11_MIN_EXEC_SCORE", 84.0):
        blockers.append(f"V11 分数不足 {score:.1f}")
    if rr < env_float("V11_MIN_EXEC_RR", max(settings.min_rr, 1.9)):
        blockers.append(f"V11 RR 不足 {rr:.2f}")

    min_edge_n = env_int("V11_MIN_EDGE_TRADES", 20 if settings.trading_mode == "paper" else 40)
    min_edge_exp = env_float("V11_MIN_EDGE_EXPECTANCY_R", 0.00 if settings.trading_mode == "paper" else 0.04)
    min_edge_pf = env_float("V11_MIN_EDGE_PF", 1.00 if settings.trading_mode == "paper" else 1.12)
    if edge_n >= min_edge_n:
        if edge_exp < min_edge_exp or edge_pf < min_edge_pf:
            blockers.append(f"V11 Edge 不达标 n={edge_n} exp={edge_exp:.3f} pf={edge_pf:.2f}")
    else:
        warnings.append(f"V11 Edge 样本不足 n={edge_n}；仅适合 paper 观察")
        if settings.trading_mode in {"demo", "live"} and env_bool("V11_REQUIRE_EDGE_FOR_EXCHANGE", True):
            blockers.append(f"V11 demo/live 要求 Edge 样本 >= {min_edge_n}")

    if not symbol_is_major(symbol):
        if score < env_float("V11_ALT_MIN_SCORE", 88.0):
            blockers.append(f"V11 山寨币分数不足 {score:.1f}")
        if setup == "breakout" and not env_bool("V11_ALLOW_ALT_BREAKOUT", False):
            blockers.append("V11 禁止山寨币 breakout 追单")
    if settings.trading_mode == "live" and not _v11_live_allowed(symbol):
        blockers.append("V11 live symbol 不在实盘白名单/major 规则内")

    status = "EXECUTABLE" if not blockers else ("PAPER_ONLY" if settings.trading_mode == "paper" and all("risk_approved=false" not in b for b in blockers) else "REJECT")
    q_score = round(max(0.0, min(100.0, score + clamp(edge_exp * 20.0, -8.0, 8.0) + clamp(rr - 1.8, 0, 1.2) * 4.0 - len(blockers) * 12.0)), 2)
    return {"status": status, "blockers": blockers, "warnings": warnings, "quality_score": q_score}


class RiskGovernorV11(RiskGovernorV10):
    """最终 AI 风控关口：默认保守，实盘必须满足研究质量门。"""

    def approve(self, decision: AIDecision, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        if env_bool("GLOBAL_KILL_SWITCH", False) or env_bool("NO_NEW_TRADES", False):
            return None, ["V11 全局熔断/禁止新仓已开启"]
        candidate, notes = super().approve(decision, payload)
        if not candidate:
            return candidate, notes
        blockers = list(notes)
        gate = candidate.get("v11_gate") or _v11_candidate_gate(candidate, self.settings)
        if gate.get("blockers"):
            blockers.extend([str(x) for x in gate.get("blockers", [])])
        if self.settings.trading_mode in {"demo", "live"} and env_bool("REQUIRE_BACKTEST_QUALITY_GATE", True):
            rq = _v11_assess_research_quality(self.settings)
            if not rq.get("passed"):
                blockers.append(f"V11 研究质量门未通过 score={rq.get('quality_score')}，禁止 exchange 执行")
        if self.settings.trading_mode == "live":
            if env_str("V11_LIVE_FINAL_CONFIRM", "").upper() != "YES":
                blockers.append("V11 live 需要 V11_LIVE_FINAL_CONFIRM=YES")
            plan = candidate.get("plan") if isinstance(candidate.get("plan"), dict) else {}
            if safe_float(plan.get("notional"), 0.0) > env_float("V11_LIVE_MAX_NOTIONAL_HARD", env_float("LIVE_MAX_ORDER_NOTIONAL", 50.0)):
                blockers.append("V11 live 订单名义价值超过硬上限")
        if blockers:
            return None, list(dict.fromkeys([str(x) for x in blockers if str(x).strip()]))
        return candidate, ["V11 institutional governor 最终通过"]


class BrainV11(BrainV10):
    """面向模拟盘交付的最终架构版本：AI 只排序，风控最终否决。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        setup_logging(settings)
        self.client = BinanceFuturesClientV9(settings)
        self.journal = TradeJournal(settings.journal_file)
        if env_bool("V11_SKIP_SYMBOL_RULES_ON_START", False) and settings.trading_mode != "live":
            self.rules = {}
            logging.warning("V11 按配置跳过启动时 symbol_rules，用于离线审计/模型卡。")
        else:
            try:
                self.rules = self.client.symbol_rules()
            except Exception as exc:
                if settings.trading_mode == "live":
                    raise
                logging.warning("V11 读取交易规则失败，paper/报告模式继续；需要真实扫描/下单时必须联网: %s", exc)
                self.rules = {}
        self.scanner = MarketScannerV7(settings, self.client)
        self.strategy = StrategyEngineV10(settings, self.client)
        self.risk = RiskManagerV10(settings, self.rules)
        self.backtester = BacktesterV8(settings, self.client)
        self.broker: Any = PaperBroker(settings, self.journal) if settings.trading_mode == "paper" else ExchangeBrokerV6(settings, self.client, self.journal, self.rules)
        self.ai_layer = AiDecisionLayer(settings)
        self.governor = RiskGovernorV11(settings)
        self.ai_candidates_file = settings.workspace / "ai_candidates.json"
        self.ai_prompt_file = settings.workspace / "aipro_prompt.md"
        self.ai_decisions_log = settings.workspace / "ai_decisions.jsonl"
        self.process_file = settings.workspace / "process_dashboard.json"
        self.daily_report_file = settings.workspace / "daily_report.md"
        self.circuit = SystemCircuitBreaker(settings)
        self.health = HealthMonitor(settings, self.client, self.broker)
        self.auditor = ExecutionAuditor(settings, self.broker)
        self.portfolio_risk = PortfolioRiskEngine(settings)
        self.v8_dashboard_file = settings.workspace / "v11_risk_dashboard.json"
        self.cost_report_file = settings.workspace / "cost_model_report.json"
        self.parameter_report_file = settings.workspace / "parameter_governance.json"
        self.v9_report_file = settings.workspace / "v9_strategy_quality_report.json"
        self.v10_report_file = settings.workspace / "v10_aipro_strategy_report.json"
        self.v11_report_file = settings.workspace / "v11_final_audit.json"
        self.v11_model_card_file = settings.workspace / "v11_model_card.md"
        self.aipro_deploy_prompt_file = settings.workspace / "aipro_deployment_prompt.md"

    def build_ai_payload(self, regime: MarketRegime, signals: Sequence[TradeSignal], marks: Dict[str, float]) -> Dict[str, Any]:
        payload = super().build_ai_payload(regime, signals, marks)
        payload["schema"] = "brain_v11_institutional_ai_candidates_v1"
        payload.setdefault("process", {})["version"] = "V11"
        payload["institutional_contract"] = {
            "ai_role": "rank_and_explain_only",
            "execution_authority": "RiskGovernorV11",
            "default_action": "NO_TRADE",
            "model_risk_controls": ["research_quality_gate", "edge_memory_gate", "live_symbol_gate", "audit_log", "kill_switch"],
            "prohibited_ai_actions": ["invent_symbol", "change_qty", "change_stop_loss", "remove_protection", "increase_leverage", "ignore_blockers"],
        }
        research_quality = _v11_assess_research_quality(self.settings)
        payload["research_quality"] = research_quality
        for row in payload.get("candidates", []):
            gate = _v11_candidate_gate(row, self.settings)
            row["v11_gate"] = gate
            row["live_ready"] = bool(row.get("live_ready")) and not gate.get("blockers") and research_quality.get("passed", False)
            row["institutional_priority"] = round(safe_float(row.get("ai_priority"), 0.0) + safe_float(gate.get("quality_score"), 0.0) * 0.12, 4)
        payload["candidates"].sort(key=lambda r: (str(r.get("v11_gate", {}).get("status")) == "EXECUTABLE", safe_float(r.get("institutional_priority"), 0.0)), reverse=True)
        _atomic_write_json(self.ai_candidates_file, payload)
        self._write_aipro_prompt(payload)
        self._write_process_dashboard(payload)
        self.write_v11_model_card()
        self.write_v11_final_audit(payload=payload, run_health=False)
        return payload

    def _write_aipro_prompt(self, payload: Dict[str, Any]) -> None:
        super()._write_aipro_prompt(payload)
        extra = """

## V11 机构级 AI 风控协议

你不是自由交易员，你是候选排序器与风险解释器。最终执行权属于 RiskGovernorV11。

强制规则：
1. 只能选择 `v11_gate.status=EXECUTABLE`、`risk_approved=true`、`process_tier` 为 A/B 的候选。
2. 如果 `research_quality.passed=false`，demo/live 必须输出 NO_TRADE；paper 也只能观察，不要强行交易。
3. 不允许修改 entry、stop_loss、tp1、tp2、qty、risk_usdt、leverage。
4. `GLOBAL_KILL_SWITCH`、`NO_NEW_TRADES`、大盘 CHAOS、严重新闻风险、EdgeMemory 负期望时必须 NO_TRADE。
5. 如果候选之间质量接近，优先 BTC/ETH/BNB/SOL 等高流动性 major，少选山寨突破。
6. 只输出 JSON，不输出散文。

输出 JSON：
```json
{
  "decision": "TRADE 或 NO_TRADE",
  "symbol": "候选 symbol，NO_TRADE 留空",
  "direction": "LONG 或 SHORT，NO_TRADE 留空",
  "confidence": 0.0,
  "reason": ["最多5条"],
  "risk_notes": ["最多5条"]
}
```
"""
        with self.ai_prompt_file.open("a", encoding="utf-8") as f:
            f.write(extra)

    def write_v11_model_card(self) -> Path:
        text = f"""# Brain V11 Model Card / 策略模型卡

生成时间：{utc_now().isoformat()}

## 用途
Brain V11 是 Binance USDⓈ-M Futures 的 AI 辅助量化交易框架，目标是先做模拟盘验证。它不保证盈利。

## AI 边界
- AI 只能从量化系统生成的候选中排序和解释。
- AI 不能创造交易对，不能修改入场、止损、止盈、仓位或杠杆。
- RiskGovernorV11 拥有最终否决权。

## 核心策略
- 趋势回踩 pullback 为主。
- 压缩后放量突破 breakout 为辅。
- 纯 trend 追单默认禁用。
- 山寨币、逆大盘、EdgeMemory 样本不足时降级或拒绝。

## 风控
- 单笔风险预算、最大持仓、日亏损熔断、全局 kill switch、研究质量门、EdgeMemory 门、实盘白名单。
- demo/live 默认要求回测/压力测试通过。

## 已知限制
- 未经真实 Binance 历史数据和模拟盘长期验证前，不应实盘。
- 新闻/事件输入依赖外部文件质量。
- 盘口滑点、资金费率和极端行情只能近似建模。
"""
        self.v11_model_card_file.write_text(text, encoding="utf-8")
        return self.v11_model_card_file

    def write_v11_final_audit(self, payload: Optional[Dict[str, Any]] = None, run_health: bool = False) -> Path:
        rq = _v11_assess_research_quality(self.settings)
        health = {}
        if run_health:
            try:
                health = self.run_healthcheck()
            except Exception as exc:
                health = {"ok": False, "error": str(exc)}
        src = payload or _v11_load_json(self.ai_candidates_file, {})
        candidates = src.get("candidates", []) if isinstance(src, dict) else []
        executable = [c for c in candidates if str(c.get("v11_gate", {}).get("status")) == "EXECUTABLE"]
        paper_only = [c for c in candidates if str(c.get("v11_gate", {}).get("status")) == "PAPER_ONLY"]
        blocked = [c for c in candidates if str(c.get("v11_gate", {}).get("status")) == "REJECT"]
        readiness_points = 0
        readiness_points += 25 if rq.get("passed") else 0
        readiness_points += 15 if self.settings.trading_mode == "paper" else 10
        readiness_points += 15 if len(executable) > 0 else 0
        readiness_points += 15 if not env_bool("GLOBAL_KILL_SWITCH", False) else 0
        readiness_points += 10 if self.ai_prompt_file.exists() else 0
        readiness_points += 10 if self.v11_model_card_file.exists() else 0
        readiness_points += 10 if self.settings.risk_per_trade <= env_float("V11_MAX_RECOMMENDED_RISK_PER_TRADE", 0.005) else 0
        report = {
            "time": utc_now().isoformat(),
            "version": "V11",
            "mode": self.settings.trading_mode,
            "readiness_score": readiness_points,
            "readiness_label": "paper_ready" if readiness_points >= 65 else "needs_more_validation",
            "research_quality": rq,
            "candidate_summary": {"total": len(candidates), "executable": len(executable), "paper_only": len(paper_only), "blocked": len(blocked)},
            "health": health,
            "files": {
                "ai_candidates": str(self.ai_candidates_file),
                "aipro_prompt": str(self.ai_prompt_file),
                "model_card": str(self.v11_model_card_file),
                "trades": str(self.settings.journal_file),
                "metrics": str(self.settings.metrics_file),
            },
            "go_no_go": {
                "paper": readiness_points >= 50,
                "demo": bool(rq.get("passed")) and readiness_points >= 70,
                "live": False,
                "live_note": "live 必须在真实历史回测、压力测试、demo/paper 长期稳定后人工开启。",
            },
        }
        _atomic_write_json(self.v11_report_file, report)
        return self.v11_report_file

    def write_aipro_deployment_prompt(self) -> Path:
        text = """# Brain V11 / Binance Ai Pro 最终部署提示词

你将在 Binance Ai Pro 的隔离 AI 子账户环境中运行 Brain V11。先使用 paper 或 demo。不要直接 live。

## 角色边界
你不是自由交易员。你只能运行脚本、读取 `ai_candidates.json`，并从候选中选择；你不能修改止损、止盈、仓位、杠杆或交易对。

## 推荐 paper 配置
```bash
export TRADING_MODE=paper
export AI_DECISION_MODE=rule
export BRAIN_WS=./brain_v11_data
export SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
export SCORE_THRESHOLD=80
export V11_MIN_EXEC_SCORE=84
export RISK_PER_TRADE=0.003
export MAX_POSITIONS=1
export MAX_NEW_ENTRIES_PER_CYCLE=1
export ENABLE_TREND_SETUP=0
export V11_LIVE_MAJOR_ONLY=1
```

## 推荐运行顺序
```bash
python3 brain_v11_aipro.py --show-config
python3 brain_v11_aipro.py --scan
python3 brain_v11_aipro.py --final-audit
python3 brain_v11_aipro.py --once
```

## NO_TRADE 条件
- 无 `v11_gate.status=EXECUTABLE` 候选。
- `research_quality.passed=false` 且当前为 demo/live。
- 任何候选含 risk_blockers。
- 大盘 CHAOS、严重新闻风险、EdgeMemory 负期望、API 异常、全局熔断开启。

## 输出格式
只输出 JSON：
```json
{"decision":"TRADE 或 NO_TRADE","symbol":"","direction":"","confidence":0.0,"reason":[],"risk_notes":[]}
```
"""
        self.aipro_deploy_prompt_file.write_text(text, encoding="utf-8")
        return self.aipro_deploy_prompt_file

# =============================================================================
# V8 输出与主入口
# =============================================================================


def print_ai_files(app: BrainV8) -> None:
    print(f"AI candidates:      {app.ai_candidates_file}")
    print(f"Ai Pro prompt:       {app.ai_prompt_file}")
    print(f"AI decisions:        {app.ai_decisions_log}")
    print(f"AI input file:       {app.ai_layer.external_file}")
    print(f"V8 risk dashboard:   {app.v8_dashboard_file}")
    print(f"Healthcheck:         {app.health.path}")
    print(f"Execution audit:     {app.auditor.path}")
    if hasattr(app, "v9_report_file"):
        print(f"V9 strategy report:  {app.v9_report_file}")
    if hasattr(app, "v10_report_file"):
        print(f"V10 strategy report: {app.v10_report_file}")
    if hasattr(app, "aipro_deploy_prompt_file"):
        print(f"AiPro deploy prompt: {app.aipro_deploy_prompt_file}")


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Brain V11 / Institutional AI Quant Final OS")
    parser.add_argument("--scan", action="store_true", help="只扫描，不开仓，并导出 ai_candidates/aipro_prompt")
    parser.add_argument("--once", action="store_true", help="运行一轮：管理持仓 + 扫描 + AI/量化决策 + 可选开仓")
    parser.add_argument("--loop", action="store_true", help="循环运行")
    parser.add_argument("--positions", action="store_true", help="查看持仓")
    parser.add_argument("--backtest", action="store_true", help="对当前 universe 回测，并输出增强 metrics/trades/edge_memory")
    parser.add_argument("--optimize", action="store_true", help="Walk-forward 参数验证，输出 optimization_results.json")
    parser.add_argument("--stress-test", action="store_true", help="基于回测交易做 Monte Carlo 压力测试")
    parser.add_argument("--healthcheck", action="store_true", help="检查配置、API、时钟、broker 和风控状态")
    parser.add_argument("--reconcile", action="store_true", help="生成执行审计/交易偏差报告")
    parser.add_argument("--risk-dashboard", action="store_true", help="输出 V8 风控仪表盘")
    parser.add_argument("--parameter-report", action="store_true", help="输出参数治理报告")
    parser.add_argument("--show-config", action="store_true", help="打印配置")
    parser.add_argument("--edge-report", action="store_true", help="查看 Edge Memory 中历史正期望 setup")
    parser.add_argument("--ai-files", action="store_true", help="显示 AI 候选、Prompt、决策日志路径")
    parser.add_argument("--final-audit", action="store_true", help="生成 V11 最终上线/模拟盘审计报告")
    parser.add_argument("--model-card", action="store_true", help="生成 V11 策略模型卡")
    parser.add_argument("--daily-report", action="store_true", help="生成每日交易/流程报告")
    parser.add_argument("--download-history", action="store_true", help="从 Binance Vision 下载公开历史 K线 ZIP 到本地缓存，不需要 API Key")
    parser.add_argument("--history-start", default=env_str("HISTORY_START", "2026-01-01"), help="历史数据下载开始日期 YYYY-MM-DD")
    parser.add_argument("--history-end", default=env_str("HISTORY_END", utc_day()), help="历史数据下载结束日期 YYYY-MM-DD")
    parser.add_argument("--history-interval", default=env_str("HISTORY_INTERVAL", "15m"), help="历史数据周期，如 15m/1h/4h")
    parser.add_argument("--history-dir", default=env_str("LOCAL_KLINE_DIR", env_str("BINANCE_HISTORY_DIR", "./binance_history")), help="历史数据保存目录")
    parser.add_argument("--limit", type=int, default=15, help="扫描表输出数量")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if args.model_card or args.final_audit or args.ai_files:
        os.environ.setdefault("V11_SKIP_SYMBOL_RULES_ON_START", "1")
    if args.show_config:
        extra = settings.sanitized()
        extra.update({
            "AI_DECISION_MODE": env_str("AI_DECISION_MODE", "rule"),
            "AI_DECISION_FILE": env_str("AI_DECISION_FILE", str(settings.workspace / "ai_decision_input.json")),
            "AI_MIN_CONFIDENCE": env_float("AI_MIN_CONFIDENCE", 0.62),
            "LIVE_MAX_ORDER_NOTIONAL": env_float("LIVE_MAX_ORDER_NOTIONAL", 0.0),
            "LIVE_ALLOWED_SYMBOLS": env_str("LIVE_ALLOWED_SYMBOLS", ""),
            "LIVE_FORCE_EDGE_MEMORY": env_bool("LIVE_FORCE_EDGE_MEMORY", False),
            "REQUIRE_BACKTEST_QUALITY_GATE": env_bool("REQUIRE_BACKTEST_QUALITY_GATE", False if settings.trading_mode == "paper" else True),
            "GLOBAL_KILL_SWITCH": env_bool("GLOBAL_KILL_SWITCH", False),
            "MAX_SAME_DIRECTION_POSITIONS": env_int("MAX_SAME_DIRECTION_POSITIONS", 2),
            "MAX_SAME_SECTOR_POSITIONS": env_int("MAX_SAME_SECTOR_POSITIONS", 1),
            "LOCAL_KLINE_DIR": env_str("LOCAL_KLINE_DIR", env_str("BINANCE_HISTORY_DIR", "")),
            "ENABLE_TREND_SETUP": env_bool("ENABLE_TREND_SETUP", False),
            "SETUP_WHITELIST": env_str("SETUP_WHITELIST", "pullback,breakout"),
            "ALT_EXTRA_SCORE": env_float("ALT_EXTRA_SCORE", 6.0),
            "ALT_SIZE_SCALE": env_float("ALT_SIZE_SCALE", 0.65),
            "V10_STRICT_MARKET_ALIGNMENT": env_bool("V10_STRICT_MARKET_ALIGNMENT", True),
            "V10_AI_EXEC_MIN_SCORE": env_float("V10_AI_EXEC_MIN_SCORE", 82.0),
            "V10_ALLOW_ALT_BREAKOUT": env_bool("V10_ALLOW_ALT_BREAKOUT", False),
            "V10_LIVE_INITIAL_SIZE_SCALE": env_float("V10_LIVE_INITIAL_SIZE_SCALE", 0.50),
            "V11_MIN_EXEC_SCORE": env_float("V11_MIN_EXEC_SCORE", 84.0),
            "V11_MIN_RESEARCH_TRADES": env_int("V11_MIN_RESEARCH_TRADES", 120),
            "V11_MIN_EXPECTANCY_R": env_float("V11_MIN_EXPECTANCY_R", 0.03),
            "V11_MIN_PROFIT_FACTOR": env_float("V11_MIN_PROFIT_FACTOR", 1.12),
            "V11_LIVE_MAJOR_ONLY": env_bool("V11_LIVE_MAJOR_ONLY", True),
            "V11_SETUP_WHITELIST": env_str("V11_SETUP_WHITELIST", "pullback,breakout"),
        })
        print_json(extra)
        return 0
    if args.download_history:
        syms = settings.symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        dest = Path(args.history_dir).expanduser().resolve()
        manifest = download_binance_vision_monthly(syms, args.history_interval, args.history_start, args.history_end, dest)
        print_json(manifest)
        print(f"\n下载完成。回测时设置：export LOCAL_KLINE_DIR={dest}")
        return 0

    if args.edge_report:
        data = EdgeMemory(settings.workspace / "edge_memory.json").load().get("keys", {})
        rows = list(data.items())
        rows.sort(key=lambda kv: (safe_float(kv[1].get("expectancy_R"), 0.0), safe_float(kv[1].get("profit_factor"), 0.0), int(kv[1].get("trades", 0))), reverse=True)
        print_edge_report(rows[: args.limit])
        return 0

    app = BrainV11(settings)
    logging.info("Brain V11 started mode=%s ai_mode=%s workspace=%s", settings.trading_mode, app.ai_layer.mode, settings.workspace)

    if args.ai_files:
        print_ai_files(app)
        return 0
    if args.model_card:
        print(f"model card: {app.write_v11_model_card()}")
        return 0
    if args.final_audit:
        app.write_v11_model_card()
        app.write_aipro_deployment_prompt()
        path = app.write_v11_final_audit(run_health=False)
        print_json(_read_json_safe(path, {}))
        return 0
    if args.healthcheck:
        print_json(app.run_healthcheck())
        return 0
    if args.reconcile:
        print_json(app.run_reconcile())
        return 0
    if args.risk_dashboard:
        app.write_v8_risk_dashboard()
        print_json(_read_json_safe(app.v8_dashboard_file, {}))
        return 0
    if args.parameter_report:
        path = app.write_parameter_governance()
        print(f"parameter governance: {path}")
        return 0
    if args.stress_test:
        print_json(app.run_stress_test())
        return 0
    if args.daily_report:
        path = app.write_daily_report()
        print(f"daily report: {path}")
        return 0
    if args.positions:
        app.print_positions()
        return 0
    if args.scan:
        regime, signals = app.scan()
        marks = app.mark_prices_for(signals)
        app.build_ai_payload(regime, signals, marks)
        print_scan_table(signals, args.limit)
        print_ai_files(app)
        return 0
    if args.backtest:
        trades, metrics = app.run_backtest()
        print_metrics(metrics)
        print(f"\ntrades csv: {settings.backtest_file}")
        print(f"metrics json: {settings.metrics_file}")
        print(f"cost model: {app.cost_report_file}")
        print(f"edge memory: {settings.workspace / 'edge_memory.json'}")
        return 0
    if args.optimize:
        best, _results = app.run_optimize()
        app.write_parameter_governance()
        print("\nBest walk-forward params")
        print_json(best)
        print(f"\noptimization json: {settings.workspace / 'optimization_results.json'}")
        print(f"parameter governance: {app.parameter_report_file}")
        return 0
    if args.loop:
        while True:
            try:
                signals = app.run_once(execute=True)
                print_scan_table(signals, args.limit)
            except KeyboardInterrupt:
                logging.info("收到中断，退出。")
                return 0
            except Exception:
                logging.error("主循环异常\n%s", traceback.format_exc())
            time.sleep(settings.loop_seconds)

    signals = app.run_once(execute=True)
    print_scan_table(signals, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

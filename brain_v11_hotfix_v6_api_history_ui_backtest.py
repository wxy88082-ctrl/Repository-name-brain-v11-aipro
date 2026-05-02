#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brain V11 Hotfix V6
- Demo-fapi API request hard timeout / Connection: close to reduce CLOSE-WAIT lockups
- Runtime heartbeat/stale markers
- Dashboard recent trades and position history: compact + collapsible
- Deduplicate closed-position realized PnL so one close is counted once
- Add small offline backtest smoke/diagnostic launcher scripts

This script is designed to be idempotent and conservative. It backs up files before editing.
"""
from __future__ import annotations

import os
import re
import json
import shutil
import py_compile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(os.environ.get("BRAIN_ROOT", "/root/brain"))
DATA_DIR = ROOT / "brain_demo_data"
TS = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
BACKUP_DIR = ROOT / f"backup_v6_{TS}"

MAIN = ROOT / "brain_v11_1_aipro.py"
DASH = ROOT / "dashboard.py"
BT = ROOT / "bt_analyze.py"
ENV = ROOT / ".demo.env"
TOOLS = ROOT / "v6_history_tools.py"
REPORT = ROOT / "V6_HOTFIX_REPORT.md"

MARK_MAIN_START = "# === V6_API_RUNTIME_GUARD_START ==="
MARK_MAIN_END = "# === V6_API_RUNTIME_GUARD_END ==="
MARK_DASH_START = "# === V6_DASHBOARD_COMPACT_HISTORY_START ==="
MARK_DASH_END = "# === V6_DASHBOARD_COMPACT_HISTORY_END ==="


def log(msg: str) -> None:
    print(f"[V6] {msg}")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def write(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def backup(p: Path) -> None:
    if not p.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, BACKUP_DIR / p.name)
    log(f"backup {p.name} -> {BACKUP_DIR / p.name}")


def strip_marked_block(src: str, start: str, end: str) -> str:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    return re.sub(pattern, "", src)


def find_insert_after_future_imports(src: str) -> int:
    lines = src.splitlines(True)
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and "coding" in lines[i]:
        i += 1
    if i < len(lines) and re.match(r"\s*([ruRU]{0,2})(['\"]{3})", lines[i]):
        quote = re.match(r"\s*([ruRU]{0,2})(['\"]{3})", lines[i]).group(2)
        i += 1
        while i < len(lines) and quote not in lines[i]:
            i += 1
        if i < len(lines):
            i += 1
    while i < len(lines) and (lines[i].strip() == "" or lines[i].lstrip().startswith("#")):
        i += 1
    while i < len(lines) and lines[i].startswith("from __future__ import"):
        i += 1
        while i < len(lines) and lines[i].strip() == "":
            i += 1
    return sum(len(x) for x in lines[:i])


MAIN_BLOCK = r'''
# === V6_API_RUNTIME_GUARD_START ===
# V6: hard timeout + heartbeat for Binance/demo-fapi requests.
# This block is intentionally self-contained and safe to leave enabled in DEMO mode.
try:
    import os as _v6_os
    import time as _v6_time
    import json as _v6_json
    import socket as _v6_socket
    from pathlib import Path as _v6_Path

    _V6_DATA_DIR = _v6_Path(_v6_os.environ.get("BRAIN_DATA_DIR", "/root/brain/brain_demo_data"))
    _V6_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _V6_HB_PATH = _V6_DATA_DIR / "runtime_heartbeat_v6.json"
    _V6_API_EVENTS = _V6_DATA_DIR / "api_events_v6.log"

    _V6_CONNECT_TIMEOUT = float(_v6_os.environ.get("BINANCE_API_CONNECT_TIMEOUT", "5"))
    _V6_READ_TIMEOUT = float(_v6_os.environ.get("BINANCE_API_READ_TIMEOUT", "12"))
    _V6_SOCKET_TIMEOUT = float(_v6_os.environ.get("BINANCE_API_SOCKET_TIMEOUT", "15"))

    _v6_socket.setdefaulttimeout(_V6_SOCKET_TIMEOUT)

    def v6_runtime_heartbeat(stage="alive", **extra):
        try:
            payload = {
                "ts": _v6_time.time(),
                "iso": _v6_time.strftime("%Y-%m-%dT%H:%M:%SZ", _v6_time.gmtime()),
                "stage": stage,
                "pid": _v6_os.getpid(),
            }
            payload.update(extra)
            _V6_HB_PATH.write_text(_v6_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _v6_api_event(event, url="", err=""):
        try:
            with _V6_API_EVENTS.open("a", encoding="utf-8") as f:
                f.write(_v6_json.dumps({
                    "ts": _v6_time.time(),
                    "iso": _v6_time.strftime("%Y-%m-%dT%H:%M:%SZ", _v6_time.gmtime()),
                    "event": event,
                    "url": str(url)[:220],
                    "err": str(err)[:500],
                    "pid": _v6_os.getpid(),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    v6_runtime_heartbeat("imported_v6_runtime_guard")

    try:
        import requests as _v6_requests
        if not getattr(_v6_requests.sessions.Session.request, "_brain_v11_v6_patched", False):
            _v6_orig_request = _v6_requests.sessions.Session.request

            def _v6_guarded_request(self, method, url, **kwargs):
                kwargs.setdefault("timeout", (_V6_CONNECT_TIMEOUT, _V6_READ_TIMEOUT))
                headers = kwargs.get("headers") or {}
                try:
                    headers.setdefault("Connection", "close")
                    kwargs["headers"] = headers
                except Exception:
                    pass
                v6_runtime_heartbeat("api_request_start", url=str(url)[:180], method=str(method))
                try:
                    resp = _v6_orig_request(self, method, url, **kwargs)
                    v6_runtime_heartbeat("api_request_ok", url=str(url)[:180], status=getattr(resp, "status_code", None))
                    return resp
                except Exception as e:
                    _v6_api_event("api_request_error", url, repr(e))
                    v6_runtime_heartbeat("api_request_error", url=str(url)[:180], error=repr(e))
                    try:
                        self.close()
                    except Exception:
                        pass
                    raise

            _v6_guarded_request._brain_v11_v6_patched = True
            _v6_requests.sessions.Session.request = _v6_guarded_request
            _v6_api_event("requests_session_patched")
    except Exception as _e:
        _v6_api_event("requests_patch_failed", err=repr(_e))

except Exception:
    pass
# === V6_API_RUNTIME_GUARD_END ===
'''


TOOLS_CODE = r'''
# -*- coding: utf-8 -*-
"""V6 dashboard helpers for compact trade/history rendering and realized-PnL de-dup."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import OrderedDict

ROOT = Path(os.environ.get("BRAIN_ROOT", "/root/brain"))
DATA_DIR = ROOT / "brain_demo_data"


def _load_json(name, default):
    p = DATA_DIR / name
    try:
        if not p.exists() or p.stat().st_size == 0:
            return default
        return json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default


def _as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("items", "rows", "trades", "data", "recent_trades", "closed_trades", "history", "records"):
            v = x.get(k)
            if isinstance(v, list):
                return v
        out = []
        for v in x.values():
            if isinstance(v, list):
                out.extend(v)
            elif isinstance(v, dict):
                out.append(v)
        return out
    return []


def _first(d, names, default=""):
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d and d.get(n) not in (None, ""):
            return d.get(n)
    lower = {str(k).lower(): k for k in d.keys()}
    for n in names:
        k = lower.get(str(n).lower())
        if k is not None and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def _num(v, default=0.0):
    try:
        if v in (None, "", "-"):
            return default
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def _ts_ms(v):
    if v in (None, ""):
        return 0
    try:
        f = float(v)
        if f > 1e12:
            return int(f)
        if f > 1e9:
            return int(f * 1000)
        return int(f)
    except Exception:
        pass
    s = str(v)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def fmt_time(v):
    ms = _ts_ms(v)
    if not ms:
        return "—"
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc) + timedelta(hours=8)
    return dt.strftime("%m-%d %H:%M CST")


def fmt_num(v, signed=False):
    f = _num(v, None)
    if f is None:
        return "—"
    if abs(f) >= 1000:
        s = f"{f:,.2f}"
    elif abs(f) >= 1:
        s = f"{f:.4f}".rstrip("0").rstrip(".")
    else:
        s = f"{f:.8f}".rstrip("0").rstrip(".")
    if signed and f > 0:
        s = "+" + s
    return s


def _trade_rows_raw():
    return _as_list(_load_json("exchange_trade_history.json", []))


def _income_rows_raw():
    return _as_list(_load_json("exchange_income_history.json", []))


def _event_type(row):
    val = str(_first(row, ["event", "type", "event_type", "side_effect", "incomeType"], "")).upper()
    if "REALIZED" in val or "CLOSE" in val or "平仓" in val or "结算" in val:
        return "平仓/结算"
    if "OPEN" in val or "开仓" in val:
        return "开仓"
    if "TRADE" in val or "成交" in val:
        return "成交"
    return val or "成交"


def compact_recent_trades(limit=30):
    out = []
    seen = set()
    for r in _trade_rows_raw():
        if not isinstance(r, dict):
            continue
        symbol = str(_first(r, ["symbol", "s"], "—"))
        t = _first(r, ["time", "ts", "timestamp", "updateTime", "T"], "")
        order_id = str(_first(r, ["orderId", "order_id", "orderID", "i"], ""))
        trade_id = str(_first(r, ["tradeId", "id", "trade_id", "t"], ""))
        key = (symbol, order_id, trade_id, _ts_ms(t), _event_type(r))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "time_fmt": fmt_time(t),
            "event": _event_type(r),
            "symbol": symbol,
            "side": str(_first(r, ["side", "direction", "positionSide", "S"], "")),
            "price": _first(r, ["price", "avgPrice", "p"], ""),
            "qty": _first(r, ["qty", "quantity", "executedQty", "q"], ""),
            "realized_pnl": _first(r, ["realizedPnl", "realized_pnl", "rp", "income", "pnl"], ""),
            "commission": _first(r, ["commission", "fee", "commissionAmount"], ""),
            "wallet": _first(r, ["walletBalance", "wallet_balance", "balance", "钱包余额"], ""),
            "equity": _first(r, ["equity", "marginBalance", "total_equity", "总权益"], ""),
            "order_id": order_id,
            "sort_ts": _ts_ms(t),
        })
    out.sort(key=lambda x: x.get("sort_ts") or 0, reverse=True)
    return out[:limit]


def dedup_closed_pnl(limit=24):
    income_rows = []
    for r in _income_rows_raw():
        if not isinstance(r, dict):
            continue
        typ = str(_first(r, ["incomeType", "type", "event"], "")).upper()
        pnl = _num(_first(r, ["income", "realizedPnl", "realized_pnl", "pnl"], ""), 0.0)
        if "REALIZED" not in typ and abs(pnl) < 1e-12:
            continue
        symbol = str(_first(r, ["symbol", "asset"], "—"))
        t = _first(r, ["time", "ts", "timestamp", "tranTime"], "")
        order_id = str(_first(r, ["orderId", "order_id", "tranId", "id"], ""))
        sec = _ts_ms(t) // 1000 if _ts_ms(t) else 0
        key = ("income", symbol, order_id or sec, round(pnl, 10))
        income_rows.append((key, {
            "time_fmt": fmt_time(t),
            "symbol": symbol,
            "direction": str(_first(r, ["side", "direction", "positionSide"], "")),
            "realized_pnl": pnl,
            "commission": _first(r, ["commission", "fee", "commissionAmount"], ""),
            "wallet": _first(r, ["walletBalance", "wallet_balance", "balance", "钱包余额"], ""),
            "equity": _first(r, ["equity", "marginBalance", "total_equity", "总权益"], ""),
            "order_id": order_id,
            "source": "income",
            "sort_ts": _ts_ms(t),
        }))

    ordered = OrderedDict()
    for key, rec in sorted(income_rows, key=lambda x: x[1].get("sort_ts") or 0, reverse=True):
        if key not in ordered:
            ordered[key] = rec

    if not ordered:
        for r in _trade_rows_raw():
            if not isinstance(r, dict):
                continue
            pnl = _num(_first(r, ["realizedPnl", "realized_pnl", "rp", "pnl"], ""), 0.0)
            if abs(pnl) < 1e-12:
                continue
            symbol = str(_first(r, ["symbol", "s"], "—"))
            t = _first(r, ["time", "ts", "timestamp", "T"], "")
            order_id = str(_first(r, ["orderId", "order_id", "i"], ""))
            trade_id = str(_first(r, ["tradeId", "id", "t"], ""))
            key = ("trade", symbol, order_id, trade_id, round(pnl, 10))
            if key in ordered:
                continue
            ordered[key] = {
                "time_fmt": fmt_time(t),
                "symbol": symbol,
                "direction": str(_first(r, ["side", "direction", "positionSide", "S"], "")),
                "realized_pnl": pnl,
                "commission": _first(r, ["commission", "fee", "commissionAmount"], ""),
                "wallet": _first(r, ["walletBalance", "wallet_balance", "balance"], ""),
                "equity": _first(r, ["equity", "marginBalance", "total_equity"], ""),
                "order_id": order_id,
                "source": "trade_fallback",
                "sort_ts": _ts_ms(t),
            }

    records = list(ordered.values())
    records.sort(key=lambda x: x.get("sort_ts") or 0, reverse=True)
    return records[:limit]


def _esc(x):
    return html.escape(str(x if x is not None else ""))


def _pnl_class(v):
    return "good" if _num(v, 0.0) >= 0 else "bad"


def render_recent_trades_html(limit_preview=5, limit_full=30):
    rows = compact_recent_trades(limit_full)
    if not rows:
        return '<div class="card"><h2>📋 最近成交</h2><div class="muted">暂无成交记录</div></div>'

    def tr(r):
        return ("<tr>"
                f"<td>{_esc(r['time_fmt'])}</td>"
                f"<td>{_esc(r['event'])}</td>"
                f"<td><b>{_esc(r['symbol'])}</b></td>"
                f"<td>{_esc(r['side'])}</td>"
                f"<td>{_esc(fmt_num(r['price']))}</td>"
                f"<td>{_esc(fmt_num(r['qty']))}</td>"
                f"<td class='{_pnl_class(r['realized_pnl'])}'>{_esc(fmt_num(r['realized_pnl'], signed=True))}</td>"
                f"<td>{_esc(fmt_num(r['commission']))}</td>"
                f"<td>{_esc(fmt_num(r['wallet']))}</td>"
                f"<td>{_esc(fmt_num(r['equity']))}</td>"
                "</tr>")

    preview = "".join(tr(r) for r in rows[:limit_preview])
    full = "".join(tr(r) for r in rows[limit_preview:])
    extra = ""
    if full:
        extra = f"<details class='v6-details'><summary>展开更多成交（{len(rows)-limit_preview} 条）</summary><table><tbody>{full}</tbody></table></details>"
    return f"""
    <div class="card v6-card">
      <h2>📋 最近成交</h2>
      <div class="muted">默认显示最近 {min(limit_preview, len(rows))} 条，展开查看更多。</div>
      <div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>事件</th><th>币对</th><th>方向</th><th>价格</th><th>数量</th><th>已实现盈亏</th><th>手续费</th><th>钱包余额</th><th>总权益</th></tr></thead>
        <tbody>{preview}</tbody>
      </table></div>
      {extra}
    </div>
    """


def render_position_history_html(limit_preview=5, limit_full=24):
    rows = dedup_closed_pnl(limit_full)
    total = sum(_num(r.get("realized_pnl"), 0.0) for r in rows)
    total_cls = "good" if total >= 0 else "bad"
    if not rows:
        return '<div class="card"><h2>📚 持仓历史 / 平仓盈亏</h2><div class="muted">暂无去重后的平仓盈亏记录</div></div>'

    def tr(r):
        return ("<tr>"
                f"<td>{_esc(r['time_fmt'])}</td>"
                f"<td><b>{_esc(r['symbol'])}</b></td>"
                f"<td>{_esc(r.get('direction',''))}</td>"
                f"<td class='{_pnl_class(r['realized_pnl'])}'>{_esc(fmt_num(r['realized_pnl'], signed=True))}</td>"
                f"<td>{_esc(fmt_num(r.get('commission')))}</td>"
                f"<td>{_esc(fmt_num(r.get('wallet')))}</td>"
                f"<td>{_esc(fmt_num(r.get('equity')))}</td>"
                f"<td>{_esc(r.get('order_id',''))}</td>"
                "</tr>")

    preview = "".join(tr(r) for r in rows[:limit_preview])
    full = "".join(tr(r) for r in rows[limit_preview:])
    extra = ""
    if full:
        extra = f"<details class='v6-details'><summary>展开更多平仓记录（{len(rows)-limit_preview} 条）</summary><table><tbody>{full}</tbody></table></details>"
    return f"""
    <div class="card v6-card">
      <h2>📚 持仓历史 / 平仓盈亏</h2>
      <div class="muted">去重后最近 {len(rows)} 条平仓/结算记录；合计已实现盈亏：<span class="{total_cls}">{fmt_num(total, signed=True)} USDT</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>时间</th><th>币对</th><th>方向</th><th>已实现盈亏</th><th>手续费</th><th>钱包余额</th><th>总权益</th><th>订单ID</th></tr></thead>
        <tbody>{preview}</tbody>
      </table></div>
      {extra}
    </div>
    """


def write_dedup_snapshot():
    rows = dedup_closed_pnl(500)
    out = DATA_DIR / "exchange_position_history_v6_dedup.json"
    try:
        out.write_text(json.dumps({"records": rows, "total_realized_pnl": sum(_num(r.get("realized_pnl"),0) for r in rows)}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return rows

if __name__ == "__main__":
    rows = write_dedup_snapshot()
    print(json.dumps({"dedup_closed_count": len(rows), "total_realized_pnl": sum(_num(r.get("realized_pnl"),0) for r in rows)}, ensure_ascii=False, indent=2))
'''


DASH_BLOCK = r'''
# === V6_DASHBOARD_COMPACT_HISTORY_START ===
# V6 overrides for compact/collapsible recent trades and de-duplicated closed PnL.
try:
    from v6_history_tools import render_recent_trades_html as _v6_render_recent_trades_html
    from v6_history_tools import render_position_history_html as _v6_render_position_history_html

    def build_trades(*args, **kwargs):
        return _v6_render_recent_trades_html(limit_preview=5, limit_full=30)

    def build_position_history(*args, **kwargs):
        return _v6_render_position_history_html(limit_preview=5, limit_full=24)

except Exception as _v6_dash_e:
    def build_trades(*args, **kwargs):
        return '<div class="card"><h2>📋 最近成交</h2><div class="bad">V6 最近成交渲染失败：%s</div></div>' % str(_v6_dash_e)

    def build_position_history(*args, **kwargs):
        return '<div class="card"><h2>📚 持仓历史 / 平仓盈亏</h2><div class="bad">V6 平仓盈亏渲染失败：%s</div></div>' % str(_v6_dash_e)
# === V6_DASHBOARD_COMPACT_HISTORY_END ===
'''


DASH_CSS = r'''
/* === V6_UI_COMPACT_CSS_START === */
.v6-card .table-wrap { overflow-x: auto; }
.v6-card table { min-width: 860px; }
.v6-details { margin-top: 10px; border-top: 1px solid rgba(148,163,184,.22); padding-top: 10px; }
.v6-details summary { cursor: pointer; color: #93c5fd; font-weight: 700; list-style: none; }
.v6-details summary::-webkit-details-marker { display: none; }
.v6-details summary::before { content: "▶ "; }
.v6-details[open] summary::before { content: "▼ "; }
.good { color: #35d07f; }
.bad { color: #ff5f6d; }
.muted { color: #94a3b8; font-size: 13px; margin: 6px 0 12px; }
/* === V6_UI_COMPACT_CSS_END === */
'''


def ensure_env():
    backup(ENV)
    existing = read(ENV) if ENV.exists() else ""
    keys = {
        "BINANCE_API_CONNECT_TIMEOUT": "5",
        "BINANCE_API_READ_TIMEOUT": "12",
        "BINANCE_API_SOCKET_TIMEOUT": "15",
        "DEMO_FAPI_REQUEST_TIMEOUT": "12",
        "V6_HTTP_CONNECTION_CLOSE": "1",
        "V6_API_STALE_MAX_SEC": "300",
        "V6_DASHBOARD_COMPACT_HISTORY": "1",
        "V6_DEDUP_CLOSED_PNL": "1",
        "BACKTEST_OFFLINE_ONLY": "1",
        "NO_API_FALLBACK": "1",
        "BACKTEST_SMOKE_LIMIT": "5000",
    }
    lines = existing.splitlines()
    for k, v in keys.items():
        pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(k)}=", re.I)
        if any(pat.search(line) for line in lines):
            continue
        lines.append(f"export {k}={v}")
    write(ENV, "\n".join(lines).rstrip() + "\n")
    log(".demo.env timeout/UI/backtest flags ensured")


def patch_main():
    if not MAIN.exists():
        log("main file not found, skip")
        return
    backup(MAIN)
    src = read(MAIN)
    src = strip_marked_block(src, MARK_MAIN_START, MARK_MAIN_END)
    idx = find_insert_after_future_imports(src)
    src = src[:idx] + "\n" + MAIN_BLOCK.strip() + "\n\n" + src[idx:]
    write(MAIN, src)
    log("brain_v11_1_aipro.py V6 runtime guard injected")


def rename_top_level_function(src: str, name: str) -> tuple[str, bool]:
    pattern = re.compile(rf"(?m)^def\s+{re.escape(name)}\s*\(")
    m = pattern.search(src)
    if not m:
        return src, False
    start = m.start()
    src = src[:start] + f"def _v6_orig_{name}(" + src[m.end():]
    return src, True


def inject_css(src: str) -> str:
    src = re.sub(r"/\* === V6_UI_COMPACT_CSS_START === \*/.*?/\* === V6_UI_COMPACT_CSS_END === \*/", "", src, flags=re.S)
    if "</style>" in src:
        return src.replace("</style>", DASH_CSS + "\n</style>", 1)
    return src + "\n# V6 CSS note: dashboard has no inline </style>; compact rendering still works via HTML details.\n"


def patch_dashboard():
    if not DASH.exists():
        log("dashboard.py not found, skip")
        return
    backup(DASH)
    src = read(DASH)
    src = strip_marked_block(src, MARK_DASH_START, MARK_DASH_END)
    src = inject_css(src)
    renamed = []
    for fn in ["build_trades", "build_position_history"]:
        src, ok = rename_top_level_function(src, fn)
        renamed.append((fn, ok))
    src = src.rstrip() + "\n\n" + DASH_BLOCK.strip() + "\n"
    write(DASH, src)
    log("dashboard.py compact/collapsible history overrides injected: " + ", ".join(f"{n}={'renamed' if ok else 'new'}" for n, ok in renamed))


def write_tools():
    backup(TOOLS)
    write(TOOLS, TOOLS_CODE)
    log("v6_history_tools.py written")


def write_backtest_scripts():
    smoke = ROOT / "run_bt_v6_smoke.sh"
    diag = ROOT / "run_bt_v6_diag.sh"
    backup(smoke)
    backup(diag)
    smoke_code = '''#!/usr/bin/env bash
set -euo pipefail
cd /root/brain
source .demo.env || true
export TRADING_MODE=paper
export BACKTEST_OFFLINE_ONLY=1
export NO_API_FALLBACK=1
export BACKTEST_START=${BACKTEST_START:-2025-01-01}
export BACKTEST_END=${BACKTEST_END:-2025-01-31}
export BACKTEST_LIMIT=${BACKTEST_SMOKE_LIMIT:-5000}
export V6_BACKTEST_SMOKE=1
mkdir -p /root/brain/backtest_v6_smoke_ws
python3 bt_analyze.py 2>&1 | tee /root/brain/backtest_v6_smoke_ws/bt_v6_smoke.log
'''
    diag_code = '''#!/usr/bin/env bash
set -euo pipefail
cd /root/brain
source .demo.env || true
export TRADING_MODE=paper
export BACKTEST_OFFLINE_ONLY=1
export NO_API_FALLBACK=1
export BACKTEST_START=${BACKTEST_START:-2025-01-01}
export BACKTEST_END=${BACKTEST_END:-2025-01-31}
export BACKTEST_LIMIT=${BACKTEST_SMOKE_LIMIT:-5000}
mkdir -p /root/brain/backtest_v6_diag_ws

declare -A CASES
CASES[A]=""
CASES[B]="ENABLE_CONFIRM_V2=0"
CASES[C]="MIN_ATR_PCT=0.03"
CASES[D]="SETUP_WHITELIST=pullback,trend,breakout ENABLE_TREND_SETUP=1"
CASES[E]="ENABLE_CONFIRM_V2=0 MIN_ATR_PCT=0.03 SCORE_THRESHOLD=50 SETUP_WHITELIST=pullback,trend,breakout ENABLE_TREND_SETUP=1"

for c in A B C D E; do
  echo "### V6 DIAG CASE $c ${CASES[$c]}"
  (
    eval "export ${CASES[$c]}"
    python3 bt_analyze.py
  ) > "/root/brain/backtest_v6_diag_ws/case_${c}.log" 2>&1 || true
  tail -n 40 "/root/brain/backtest_v6_diag_ws/case_${c}.log" || true
done
python3 - <<'PYBT'
from pathlib import Path
import json, re
base=Path('/root/brain/backtest_v6_diag_ws')
summary={}
for p in sorted(base.glob('case_*.log')):
    txt=p.read_text(errors='ignore')
    trades=None
    for pat in [r'trades\s*[:=]\s*(\d+)', r'final_trades\s*[:=]\s*(\d+)']:
        m=re.search(pat, txt, re.I)
        if m: trades=int(m.group(1))
    summary[p.stem]= {'bytes': p.stat().st_size, 'trades_hint': trades, 'tail': txt[-800:]}
(base/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2)[:4000])
PYBT
'''
    write(smoke, smoke_code)
    write(diag, diag_code)
    os.chmod(smoke, 0o755)
    os.chmod(diag, 0o755)
    log("run_bt_v6_smoke.sh and run_bt_v6_diag.sh written")


def compile_check():
    for p in [MAIN, DASH, BT, TOOLS]:
        if p.exists():
            py_compile.compile(str(p), doraise=True)
            log(f"py_compile OK: {p.name}")


def write_report():
    content = f"""# Brain V11 Hotfix V6 Report

Applied at: {datetime.now(timezone.utc).isoformat()}
Backup dir: `{BACKUP_DIR}`

## Included fixes

1. Demo-fapi/request hard timeout guard and `Connection: close` to reduce CLOSE-WAIT lockups.
2. Runtime heartbeat: `/root/brain/brain_demo_data/runtime_heartbeat_v6.json`.
3. API event log: `/root/brain/brain_demo_data/api_events_v6.log`.
4. Dashboard recent trades compact/collapsible rendering.
5. Dashboard closed position realized PnL de-duplication; income `REALIZED_PNL` is primary source.
6. Snapshot helper: `/root/brain/brain_demo_data/exchange_position_history_v6_dedup.json` generated by `python3 v6_history_tools.py`.
7. Small offline backtest launchers:
   - `run_bt_v6_smoke.sh`
   - `run_bt_v6_diag.sh`

## Important

- This patch does not cancel protection orders.
- This patch does not close positions.
- This patch does not switch to live trading.
- After applying, restart only `brain_demo` and `brain_ui`.
"""
    write(REPORT, content)
    log("V6_HOTFIX_REPORT.md written")


def main():
    log(f"root={ROOT}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_env()
    write_tools()
    patch_main()
    patch_dashboard()
    write_backtest_scripts()
    try:
        import subprocess, sys
        subprocess.run([sys.executable, str(TOOLS)], cwd=str(ROOT), timeout=10, check=False)
    except Exception as e:
        log(f"dedup snapshot generation skipped: {e}")
    compile_check()
    write_report()
    log("DONE")
    print("\nV6 hotfix applied. Restart brain_demo and brain_ui only. See V6_HOTFIX_REPORT.md")


if __name__ == "__main__":
    main()

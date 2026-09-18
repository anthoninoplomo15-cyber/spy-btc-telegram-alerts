"""
BTC EMA gap + SPY VWAP Telegram alert terminal.
Flask + background poller (~12s). Render-ready.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
POLL_INTERVAL_SEC = 12

BINANCE_KLINES_URLS = [
    # Primary (public data API per product spec); fallbacks for geo/WAF blocks
    "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=120",
    "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=120",
    "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=120",
]
YAHOO_SPY_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
    "?interval=1m&range=1d"
)

GAP_START = 20.0  # first alert at |gap| >= $20
GAP_STEP = 5.0  # then every +$5

SPY_NOISE_PCT = 0.05  # first alert only when |vs window open| >= 0.05%

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("alerts")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Shared state (status page)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state: dict = {
    "started_at": None,
    "last_poll_at": None,
    "last_error": None,
    "btc": {
        "price": None,
        "ema3": None,
        "ema9": None,
        "gap": None,
        "gap_pct": None,
        "last_alert_step": None,
        "last_alert_dir": None,  # +1 / -1
        "last_alert_msg": None,
        "last_alert_at": None,
    },
    "spy": {
        "price": None,
        "vwap": None,
        "side": None,  # "above" / "below" / None
        "window": None,  # "morning" / "afternoon" / None
        "window_open": None,
        "pct_vs_open": None,
        "noise_cleared": False,
        "last_alert_msg": None,
        "last_alert_at": None,
    },
    "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    "telegram_ok": None,
    "telegram_last_error": None,
    "alerts_paused": False,
    "btc_pullback": {
        "extreme": None,
        "last_step": None,
        "last_alert_msg": None,
        "last_alert_at": None,
    },
}


def _now_et() -> datetime:
    return datetime.now(ET)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def send_telegram(text: str) -> bool:
    """Send message via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured; skip: %s", text)
        with _state_lock:
            _state["telegram_ok"] = False
            _state["telegram_last_error"] = "missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
        return False

    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": "1"}
    ).encode()
    req = urllib.request.Request(
        api,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        data = json.loads(raw.decode())
        ok = bool(data.get("ok"))
        with _state_lock:
            _state["telegram_ok"] = ok
            _state["telegram_last_error"] = None if ok else str(data)
        if not ok:
            log.error("Telegram API error: %s", data)
        else:
            log.info("Telegram sent: %s", text)
        return ok
    except Exception as e:
        log.exception("Telegram send failed")
        with _state_lock:
            _state["telegram_ok"] = False
            _state["telegram_last_error"] = str(e)
        return False


# ---------------------------------------------------------------------------
# EMA helpers (seed with SMA, then EMA)
# ---------------------------------------------------------------------------

def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[:period]) / period


def _ema_series(closes: list[float], period: int) -> list[float | None]:
    """Return EMA series aligned to closes; None until SMA seed is ready."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(closes)):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# BTC: Binance klines + gap alerts
# ---------------------------------------------------------------------------

# Track last fired absolute step and direction so we don't spam
_btc_alert_dir: int | None = None  # +1 bullish gap, -1 bearish
_btc_alert_step: float | None = None  # last |gap| threshold fired (20, 25, ...)

# Pullback tracking (armed after a gap alert on a side)
PULLBACK_STEP = 5.0
_btc_pullback_extreme: float | None = None  # high (bull) / low (bear) since armed
_btc_pullback_step: float | None = None  # last $5 pullback step fired (5, 10, ...)

# Global pause: skip Telegram sends; keep polling/state updates
_alerts_paused = False


def fetch_btc_closes() -> tuple[list[float], float]:
    """Return (closes, last_close) from Binance 1m klines (with URL fallbacks)."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; spy-btc-telegram-alerts/1.0)"}
    last_err: Exception | None = None
    for url in BINANCE_KLINES_URLS:
        try:
            raw = _http_get(url, headers=headers)
            data = json.loads(raw.decode())
            closes = [float(c[4]) for c in data]
            if not closes:
                raise RuntimeError("empty Binance klines")
            return closes, closes[-1]
        except Exception as e:
            last_err = e
            log.warning("Binance fetch failed (%s): %s", url.split("?")[0], e)
    raise RuntimeError(f"all Binance endpoints failed: {last_err}")


def btc_gap_step(abs_gap: float) -> float | None:
    """Highest $5 step at or below abs_gap, starting at $20. None if < $20."""
    if abs_gap < GAP_START:
        return None
    # steps: 20, 25, 30, ...
    n = int((abs_gap - GAP_START) // GAP_STEP)
    return GAP_START + n * GAP_STEP


def _reset_btc_pullback() -> None:
    global _btc_pullback_extreme, _btc_pullback_step
    _btc_pullback_extreme = None
    _btc_pullback_step = None
    with _state_lock:
        _state["btc_pullback"]["extreme"] = None
        _state["btc_pullback"]["last_step"] = None


def _alerts_are_paused() -> bool:
    with _state_lock:
        return bool(_state.get("alerts_paused"))


def process_btc() -> None:
    global _btc_alert_dir, _btc_alert_step
    global _btc_pullback_extreme, _btc_pullback_step

    closes, price = fetch_btc_closes()
    ema3s = _ema_series(closes, 3)
    ema9s = _ema_series(closes, 9)
    ema3 = ema3s[-1]
    ema9 = ema9s[-1]
    if ema3 is None or ema9 is None:
        with _state_lock:
            _state["btc"]["price"] = price
            _state["btc"]["ema3"] = ema3
            _state["btc"]["ema9"] = ema9
            _state["btc"]["gap"] = None
        return

    gap = ema3 - ema9
    abs_gap = abs(gap)
    direction = 1 if gap > 0 else (-1 if gap < 0 else 0)
    gap_pct = (abs_gap / price * 100.0) if price else None

    with _state_lock:
        _state["btc"]["price"] = price
        _state["btc"]["ema3"] = ema3
        _state["btc"]["ema9"] = ema9
        _state["btc"]["gap"] = gap
        _state["btc"]["gap_pct"] = gap_pct

    if direction == 0:
        return

    # Direction flip → reset gap + pullback; require $20 on new side
    if _btc_alert_dir is not None and direction != _btc_alert_dir:
        log.info("BTC gap direction flip %s -> %s; reset", _btc_alert_dir, direction)
        _btc_alert_dir = None
        _btc_alert_step = None
        _reset_btc_pullback()

    step = btc_gap_step(abs_gap)
    paused = _alerts_are_paused()

    # Re-arm pullback extreme if gap side still active (e.g. after STOP cleared it)
    if (
        not paused
        and _btc_alert_dir is not None
        and _btc_alert_dir == direction
        and _btc_pullback_extreme is None
    ):
        _btc_pullback_extreme = price
        _btc_pullback_step = None
        with _state_lock:
            _state["btc_pullback"]["extreme"] = _btc_pullback_extreme
            _state["btc_pullback"]["last_step"] = None

    # --- Gap step alerts (existing $20 / +$5 logic) ---
    if step is not None:
        should_fire = False
        if _btc_alert_dir is None:
            should_fire = True
        elif step > (_btc_alert_step or 0):
            should_fire = True

        if should_fire:
            emoji = "🟢" if direction > 0 else "🔴"
            # Lower EMA: green (gap>0) → EMA9; red (gap<0) → EMA3
            lower_ema = ema9 if direction > 0 else ema3
            msg = f"{emoji} BTC GAP ${int(step)} · {int(round(lower_ema))}"

            if not paused:
                send_telegram(msg)
            else:
                log.info("Paused; skip Telegram: %s", msg)

            _btc_alert_dir = direction
            _btc_alert_step = step
            # Arm / re-arm pullback extreme on gap alert
            if _btc_pullback_extreme is None:
                _btc_pullback_extreme = price
                _btc_pullback_step = None
            else:
                # Favor the gap: bullish track high, bearish track low
                if direction > 0:
                    _btc_pullback_extreme = max(_btc_pullback_extreme, price)
                else:
                    _btc_pullback_extreme = min(_btc_pullback_extreme, price)

            with _state_lock:
                _state["btc"]["last_alert_step"] = step
                _state["btc"]["last_alert_dir"] = direction
                _state["btc"]["last_alert_msg"] = msg
                _state["btc"]["last_alert_at"] = _iso(_now_et())
                _state["btc_pullback"]["extreme"] = _btc_pullback_extreme

    # --- $5 pullback alerts (only when gap alerts are active on a side) ---
    if _btc_alert_dir is not None and _btc_alert_dir == direction and _btc_pullback_extreme is not None:
        if direction > 0:
            # Bullish: track high; pullback = extreme - price
            if price > _btc_pullback_extreme:
                _btc_pullback_extreme = price
                with _state_lock:
                    _state["btc_pullback"]["extreme"] = _btc_pullback_extreme
            pullback = _btc_pullback_extreme - price
        else:
            # Bearish: track low; pullback = price - extreme
            if price < _btc_pullback_extreme:
                _btc_pullback_extreme = price
                with _state_lock:
                    _state["btc_pullback"]["extreme"] = _btc_pullback_extreme
            pullback = price - _btc_pullback_extreme

        if pullback >= PULLBACK_STEP:
            # Highest $5 step at or below pullback: 5, 10, 15, ...
            pb_step = PULLBACK_STEP * int(pullback // PULLBACK_STEP)
            if _btc_pullback_step is None or pb_step > _btc_pullback_step:
                # Lower EMA = "safe" reference: green→EMA9, red→EMA3
                lower_ema = ema9 if direction > 0 else ema3
                pb_msg = f"⚠️ BTC pullback ${int(pb_step)} · {lower_ema:.0f}"
                if not paused:
                    send_telegram(pb_msg)
                else:
                    log.info("Paused; skip Telegram: %s", pb_msg)
                _btc_pullback_step = pb_step
                with _state_lock:
                    _state["btc_pullback"]["last_step"] = pb_step
                    _state["btc_pullback"]["last_alert_msg"] = pb_msg
                    _state["btc_pullback"]["last_alert_at"] = _iso(_now_et())
                    _state["btc_pullback"]["extreme"] = _btc_pullback_extreme


# ---------------------------------------------------------------------------
# SPY: Yahoo 1m + RTH VWAP, session windows only
# ---------------------------------------------------------------------------

_spy_noise_cleared: bool = False
_spy_last_side: str | None = None  # "above" / "below"
_spy_active_window: str | None = None  # "morning" / "afternoon"


def _in_spy_window(now: datetime) -> str | None:
    """Return 'morning' / 'afternoon' if inside alert windows on a weekday, else None."""
    if now.weekday() >= 5:  # Sat/Sun
        return None
    t = now.time()
    # 09:30–10:00 ET
    if dtime(9, 30) <= t < dtime(10, 0):
        return "morning"
    # 15:30–16:00 ET
    if dtime(15, 30) <= t < dtime(16, 0):
        return "afternoon"
    return None


def fetch_spy_bars() -> list[dict]:
    """
    Fetch SPY 1m bars from Yahoo.
    Each bar: {ts (ET datetime), open, high, low, close, volume}
    """
    raw = _http_get(
        YAHOO_SPY_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    data = json.loads(raw.decode())
    result = data["chart"]["result"][0]
    ts_list = result["timestamp"]
    q = result["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(ts_list):
        o, h, l, c, v = (
            q["open"][i],
            q["high"][i],
            q["low"][i],
            q["close"][i],
            q["volume"][i],
        )
        if None in (o, h, l, c, v):
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
        bars.append(
            {
                "ts": dt,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
            }
        )
    return bars


def _rth_vwap_and_window_open(
    bars: list[dict], window: str
) -> tuple[float | None, float | None, float | None]:
    """
    Compute RTH VWAP from 09:30 using typical price * volume.
    Also return window open price (09:30 open for morning, 15:30 open for afternoon)
    and latest close.
    """
    rth_start = dtime(9, 30)
    win_open_t = dtime(9, 30) if window == "morning" else dtime(15, 30)

    cum_tp_v = 0.0
    cum_v = 0.0
    window_open: float | None = None
    last_close: float | None = None

    for b in bars:
        t = b["ts"].time()
        # RTH roughly until 16:00; include bars from 09:30 onward for VWAP
        if t < rth_start:
            continue
        if t >= dtime(16, 0):
            continue

        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = b["volume"]
        if vol > 0:
            cum_tp_v += typical * vol
            cum_v += vol

        # First bar at or after window open time = window open
        if window_open is None and t >= win_open_t:
            window_open = b["open"]

        last_close = b["close"]

    vwap = (cum_tp_v / cum_v) if cum_v > 0 else None
    return vwap, window_open, last_close


def process_spy() -> None:
    global _spy_noise_cleared, _spy_last_side, _spy_active_window

    now = _now_et()
    window = _in_spy_window(now)

    with _state_lock:
        _state["spy"]["window"] = window

    if window is None:
        # Outside windows: clear session alert state so next window starts fresh
        if _spy_active_window is not None:
            log.info("Left SPY window %s; reset side/noise", _spy_active_window)
        _spy_active_window = None
        _spy_noise_cleared = False
        _spy_last_side = None
        with _state_lock:
            _state["spy"]["noise_cleared"] = False
            _state["spy"]["side"] = None
            _state["spy"]["pct_vs_open"] = None
        return

    # New window entered → reset noise/side
    if _spy_active_window != window:
        log.info("Entered SPY window %s", window)
        _spy_active_window = window
        _spy_noise_cleared = False
        _spy_last_side = None

    bars = fetch_spy_bars()
    if not bars:
        raise RuntimeError("empty Yahoo SPY bars")

    vwap, window_open, price = _rth_vwap_and_window_open(bars, window)
    if price is None or window_open is None or vwap is None:
        with _state_lock:
            _state["spy"]["price"] = price
            _state["spy"]["vwap"] = vwap
            _state["spy"]["window_open"] = window_open
        return

    pct_vs_open = (price - window_open) / window_open * 100.0
    side = "above" if price >= vwap else "below"

    with _state_lock:
        _state["spy"]["price"] = price
        _state["spy"]["vwap"] = vwap
        _state["spy"]["window_open"] = window_open
        _state["spy"]["pct_vs_open"] = pct_vs_open
        _state["spy"]["side"] = side
        _state["spy"]["noise_cleared"] = _spy_noise_cleared

    # Noise filter: first alert only when |vs window open| >= 0.05%
    if not _spy_noise_cleared:
        if abs(pct_vs_open) < SPY_NOISE_PCT:
            return
        _spy_noise_cleared = True
        with _state_lock:
            _state["spy"]["noise_cleared"] = True
        # Fire initial side alert
        emoji = "🟢" if pct_vs_open >= 0 else "🔴"
        sign = "+" if pct_vs_open >= 0 else ""
        msg = f"{emoji} SP {sign}{pct_vs_open:.2f}%"
        if not _alerts_are_paused():
            send_telegram(msg)
        else:
            log.info("Paused; skip Telegram: %s", msg)
        _spy_last_side = side
        with _state_lock:
            _state["spy"]["last_alert_msg"] = msg
            _state["spy"]["last_alert_at"] = _iso(_now_et())
        return

    # After noise cleared: alert only on side change vs VWAP
    if _spy_last_side is not None and side != _spy_last_side:
        emoji = "🟢" if pct_vs_open >= 0 else "🔴"
        sign = "+" if pct_vs_open >= 0 else ""
        msg = f"{emoji} SP {sign}{pct_vs_open:.2f}%"
        if not _alerts_are_paused():
            send_telegram(msg)
        else:
            log.info("Paused; skip Telegram: %s", msg)
        _spy_last_side = side
        with _state_lock:
            _state["spy"]["last_alert_msg"] = msg
            _state["spy"]["last_alert_at"] = _iso(_now_et())
    elif _spy_last_side is None:
        _spy_last_side = side


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

def _poll_once() -> None:
    errors = []
    try:
        process_btc()
    except Exception as e:
        log.exception("BTC poll failed")
        errors.append(f"btc: {e}")
    try:
        process_spy()
    except Exception as e:
        log.exception("SPY poll failed")
        errors.append(f"spy: {e}")

    with _state_lock:
        _state["last_poll_at"] = _iso(_now_et())
        _state["last_error"] = "; ".join(errors) if errors else None


def _poll_loop() -> None:
    log.info("Poller started (interval=%ss)", POLL_INTERVAL_SEC)
    while True:
        try:
            _poll_once()
        except Exception:
            log.exception("Unexpected poller error")
        time.sleep(POLL_INTERVAL_SEC)


_poller_started = False
_poller_lock = threading.Lock()


def start_poller() -> None:
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True
        with _state_lock:
            _state["started_at"] = _iso(_now_et())
        t = threading.Thread(target=_poll_loop, name="alert-poller", daemon=True)
        t.start()


# Start on import (gunicorn workers + flask run)
start_poller()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

def _fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


@app.get("/")
def status_page() -> Response:
    with _state_lock:
        s = json.loads(json.dumps(_state))  # shallow copy via json

    now = _now_et()
    window = _in_spy_window(now)
    tg_cfg = s["telegram_configured"]
    tg_ok = s["telegram_ok"]
    paused = bool(s.get("alerts_paused"))
    tg_label = (
        "configured & OK"
        if tg_cfg and tg_ok
        else ("configured (last send failed)" if tg_cfg and tg_ok is False
              else ("configured (not yet sent)" if tg_cfg else "MISSING — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"))
    )

    b = s["btc"]
    p = s["spy"]
    pb = s.get("btc_pullback") or {}
    alerts_label = "STOPPED" if paused else "ALERTS ON"
    alerts_class = "bad" if paused else "ok"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="15"/>
  <title>spy-btc-telegram-alerts</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
           background:#0b0f14; color:#e6edf3; margin:0; padding:1.5rem; }}
    h1 {{ font-size:1.25rem; margin:0 0 1rem; }}
    .grid {{ display:grid; gap:1rem; max-width:720px; }}
    .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:1rem; }}
    .k {{ color:#8b949e; }}
    .ok {{ color:#3fb950; }}
    .bad {{ color:#f85149; }}
    .warn {{ color:#d29922; }}
    table {{ width:100%; border-collapse:collapse; }}
    td {{ padding:0.25rem 0.4rem; vertical-align:top; }}
    td:first-child {{ width:42%; color:#8b949e; }}
    footer {{ margin-top:1.5rem; color:#8b949e; font-size:0.85rem; }}
    .controls {{ display:flex; flex-wrap:wrap; gap:0.75rem; align-items:center; margin-top:0.75rem; }}
    .btn {{
      font: inherit; font-weight:700; font-size:1.1rem; letter-spacing:0.04em;
      border:none; border-radius:10px; padding:0.85rem 1.4rem; cursor:pointer;
      color:#fff; min-width:8rem;
    }}
    .btn:disabled {{ opacity:0.45; cursor:not-allowed; }}
    .btn-stop {{ background:#da3633; }}
    .btn-start {{ background:#238636; }}
    .alerts-status {{ font-size:1.15rem; font-weight:700; }}
  </style>
</head>
<body>
  <h1>📡 spy-btc-telegram-alerts</h1>
  <div class="grid">
    <div class="card">
      <strong>Alerts control</strong>
      <div class="controls">
        <span id="alertsStatus" class="alerts-status {alerts_class}">{alerts_label}</span>
        <button type="button" class="btn btn-stop" id="btnStop"
                {"disabled" if paused else ""} onclick="setAlerts(false)">STOP</button>
        <button type="button" class="btn btn-start" id="btnStart"
                {"" if paused else "disabled"} onclick="setAlerts(true)">START</button>
      </div>
      <p class="k" style="margin:0.75rem 0 0;">STOP skips Telegram (BTC gap, pullbacks, SPY). Polling &amp; state keep updating.</p>
    </div>

    <div class="card">
      <strong>System</strong>
      <table>
        <tr><td>Now (ET)</td><td>{now.strftime("%Y-%m-%d %H:%M:%S %Z")}</td></tr>
        <tr><td>Started</td><td>{s.get("started_at") or "—"}</td></tr>
        <tr><td>Last poll</td><td>{s.get("last_poll_at") or "—"}</td></tr>
        <tr><td>Last error</td><td class="{"bad" if s.get("last_error") else ""}">{s.get("last_error") or "none"}</td></tr>
        <tr><td>Telegram</td><td class="{"ok" if tg_cfg and tg_ok else ("warn" if tg_cfg else "bad")}">{tg_label}</td></tr>
        <tr><td>TG last err</td><td>{s.get("telegram_last_error") or "—"}</td></tr>
        <tr><td>Poll interval</td><td>{POLL_INTERVAL_SEC}s</td></tr>
      </table>
    </div>

    <div class="card">
      <strong>BTC EMA gap (24/7)</strong>
      <table>
        <tr><td>Price</td><td>{_fmt(b.get("price"), 2)}</td></tr>
        <tr><td>EMA3</td><td>{_fmt(b.get("ema3"), 4)}</td></tr>
        <tr><td>EMA9</td><td>{_fmt(b.get("ema9"), 4)}</td></tr>
        <tr><td>gap$ (EMA3−EMA9)</td><td>{_fmt(b.get("gap"), 4)}</td></tr>
        <tr><td>|gap|/price</td><td>{_fmt(b.get("gap_pct"), 4)}%</td></tr>
        <tr><td>Last alert step</td><td>{_fmt(b.get("last_alert_step"), 0)}</td></tr>
        <tr><td>Last alert dir</td><td>{b.get("last_alert_dir") if b.get("last_alert_dir") is not None else "—"}</td></tr>
        <tr><td>Last alert</td><td>{b.get("last_alert_msg") or "—"}</td></tr>
        <tr><td>Last alert at</td><td>{b.get("last_alert_at") or "—"}</td></tr>
        <tr><td>Pullback extreme</td><td>{_fmt(pb.get("extreme"), 2)}</td></tr>
        <tr><td>Last pullback step</td><td>{_fmt(pb.get("last_step"), 0)}</td></tr>
        <tr><td>Last pullback</td><td>{pb.get("last_alert_msg") or "—"}</td></tr>
      </table>
    </div>

    <div class="card">
      <strong>SPY VWAP (weekdays 09:30–10:00 &amp; 15:30–16:00 ET)</strong>
      <table>
        <tr><td>Active window</td><td class="{"ok" if window else "warn"}">{window or "outside — no SPY alerts"}</td></tr>
        <tr><td>Price</td><td>{_fmt(p.get("price"), 4)}</td></tr>
        <tr><td>RTH VWAP</td><td>{_fmt(p.get("vwap"), 4)}</td></tr>
        <tr><td>Side vs VWAP</td><td>{p.get("side") or "—"}</td></tr>
        <tr><td>Window open</td><td>{_fmt(p.get("window_open"), 4)}</td></tr>
        <tr><td>% vs window open</td><td>{_fmt(p.get("pct_vs_open"), 4)}%</td></tr>
        <tr><td>Noise cleared (≥0.05%)</td><td>{"yes" if p.get("noise_cleared") else "no"}</td></tr>
        <tr><td>Last alert</td><td>{p.get("last_alert_msg") or "—"}</td></tr>
        <tr><td>Last alert at</td><td>{p.get("last_alert_at") or "—"}</td></tr>
      </table>
    </div>
  </div>
  <footer>Auto-refresh 15s · Binance BTCUSDT 1m · Yahoo SPY 1m · no secrets on this page</footer>
  <script>
    async function setAlerts(start) {{
      const path = start ? "/api/start" : "/api/stop";
      const btnStop = document.getElementById("btnStop");
      const btnStart = document.getElementById("btnStart");
      btnStop.disabled = true;
      btnStart.disabled = true;
      try {{
        const res = await fetch(path, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: "{{}}"
        }});
        const data = await res.json();
        const paused = !!data.alerts_paused;
        const el = document.getElementById("alertsStatus");
        el.textContent = paused ? "STOPPED" : "ALERTS ON";
        el.className = "alerts-status " + (paused ? "bad" : "ok");
        btnStop.disabled = paused;
        btnStart.disabled = !paused;
      }} catch (e) {{
        alert("Request failed: " + e);
        btnStop.disabled = false;
        btnStart.disabled = false;
      }}
    }}
  </script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


def _set_alerts_paused(paused: bool):
    global _alerts_paused
    with _state_lock:
        _alerts_paused = paused
        _state["alerts_paused"] = paused
    if paused:
        # Reset pullback tracking on stop so START does not inherit stale extremes
        _reset_btc_pullback()
        log.info("Alerts STOPPED (Telegram sends paused)")
    else:
        log.info("Alerts STARTED (Telegram sends enabled)")
    return {"ok": True, "alerts_paused": paused}


@app.post("/api/stop")
def api_stop():
    # Accept empty / JSON body
    _ = request.get_data(cache=False)
    return jsonify(_set_alerts_paused(True))


@app.post("/api/start")
def api_start():
    _ = request.get_data(cache=False)
    return jsonify(_set_alerts_paused(False))


@app.get("/health")
def health():
    return {"ok": True, "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Flask reloader would double-start the thread; disable it
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

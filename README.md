# spy-btc-telegram-alerts

Render-ready Flask terminal that sends Telegram alerts for:

1. **BTC EMA gap (24/7)** — Binance `BTCUSDT` 1m klines; `gap$ = EMA3 − EMA9`. First alert when `|gap|` crosses **$20**, then every **+$5** step in the same direction. Direction flip resets to $20 on the new side. Format includes the **lower EMA** (green → EMA9, red → EMA3), no percentage: `🟢 BTC GAP $25 · 76401` / `🔴 BTC GAP $30 · 76380`.
2. **BTC $5 pullbacks** — While gap alerts are active on a side, track the extreme BTC price favoring the gap (bullish: high; bearish: low). When price retraces **$5** from that extreme, alert `⚠️ BTC pullback $5 · <lower EMA>` (green → EMA9, red → EMA3); then every additional **+$5**. Resets on direction flip / stop.
3. **SPY VWAP (weekdays only)** — Yahoo SPY 1m, RTH VWAP from 09:30 ET. Alerts **only** in:
   - **09:30–10:00 ET**
   - **15:30–16:00 ET**  
   Noise filter: first alert when `|SPY vs window open| ≥ 0.05%`. Then alerts only on **side change** vs VWAP. Format: `🟢 SP +0.12%` / `🔴 SP -0.08%`.

**Stop / Start:** status page has big STOP / START buttons, or Telegram chat commands (same pause flag). When stopped, Telegram sends (BTC gap, pullbacks, SPY) are skipped; polling and state still update.

**Telegram commands** (only from `TELEGRAM_CHAT_ID`; others ignored):

| Command | Effect |
|---------|--------|
| `/stop` or `/pause` | Pause alerts → reply `⛔ Alertas STOP` |
| `/start` or `/go` | Resume alerts → reply `✅ Alertas ON` |
| `/status` | Reply ON/OFF + brief last gap |

The bot polls `getUpdates` every ~3s in a background thread and advances the offset so messages are not reprocessed.

No Kalshi / trading code. No secrets in the repo.

## Status page

`GET /` — last BTC gap / pullback, last SPY, active windows, Telegram config OK?, STOP/START controls (`ALERTS ON` / `STOPPED`)

`POST /api/stop` — pause Telegram alerts (JSON body optional)

`POST /api/start` — resume Telegram alerts

`GET /health` — JSON `{ok, telegram_configured}`

Telegram chat: `/stop` `/pause` `/start` `/go` `/status` (same pause as web buttons)

## Environment variables (Render)

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes (for alerts) | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Yes (for alerts) | Your user/group chat id |
| `PORT` | Set by Render | Gunicorn bind port |

If Telegram env vars are missing, the app **still runs**; the status page shows missing config and no messages are sent.

### How to get a bot token

1. Open Telegram → talk to [@BotFather](https://t.me/BotFather)
2. `/newbot` → follow prompts
3. Copy the token → set as `TELEGRAM_BOT_TOKEN`

### How to get your chat id

1. Start a chat with your bot (send `/start`)
2. Or add the bot to a group
3. Open: `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find `"chat":{"id": ...}` → set as `TELEGRAM_CHAT_ID`  
   (For groups the id is often negative.)

## Deploy on Render

1. Push this folder to a GitHub repo (or use Render Blueprint / manual upload).
2. **New → Web Service** → connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120`  
     (or leave blank and use the `Procfile`)
4. **Environment** → add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. Deploy. Open the service URL → status page should load within ~15s of first poll.

**Important:** use **1 worker** so the in-memory alert state (gap steps / SPY side) is not duplicated across processes.

## Local run

```bash
cd spy-btc-telegram-alerts
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...   # optional
export TELEGRAM_CHAT_ID=...     # optional
python app.py
# → http://127.0.0.1:5000/
```

Dry-run without tokens is fine: polls run; Telegram sends are skipped with a log warning.

## Data sources

- BTC: `https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=120` (public)
- SPY: Yahoo Finance chart API 1m (requires a browser-like `User-Agent`)

Poll interval ≈ **12 seconds**.

## License

Private / personal use for Ramon · jade.

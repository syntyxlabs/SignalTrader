# Plan: VIP Signal Auto-Trader

## Goal

Automatically execute trades on MT5 when a VIP Telegram channel sends trading signals. The bot monitors the channel 24/7, parses signals using Claude, and executes on MetaTrader 5.

## User Experience

1. VIP channel posts: "BUY NOW XAUUSD PRICE 4978 SL 4968 TP1 4980..."
2. Signal Trader detects the signal within seconds
3. Claude parses the message → structured trade data
4. MT5 executes: BUY 0.01 XAUUSD @ market, SL 4968
5. Channel posts "Edit your stop loss to TP1" → bot modifies SL to 4980
6. Channel posts "TP5 HIT!" → bot trails SL accordingly
7. User wakes up to see completed trade in MT5 history

## Architecture

```
VIP Telegram Channel
        │
        ▼
┌──────────────────┐
│  Telethon Client  │  (your personal TG account, listens to channel)
│  (channel_listener.py)
└────────┬─────────┘
         │ new message
         ▼
┌──────────────────┐
│  Signal Parser    │  Claude SDK (free via Max subscription)
│  (parser.py)      │  Classifies: SIGNAL / SL_UPDATE / TP_HIT / NOISE
└────────┬─────────┘
         │ structured action
         ▼
┌──────────────────┐
│  Trade Manager    │  Manages position lifecycle
│  (trade_manager.py)│  Open → Trail SL → Close
└────────┬─────────┘
         │ MT5 orders
         ▼
┌──────────────────┐
│  MT5 Client       │  MetaTrader5 Python package
│  (mt5_client.py)  │  Place/modify/close orders
└──────────────────┘
```

## Project Structure

```
C:/Projects/signal-trader/
├── main.py                # Entry point — starts Telethon + trade manager
├── config.json            # Channel ID, lot size, pair, safety limits
├── .env                   # Telegram API creds (api_id, api_hash, phone)
│                          # MT5 creds (login, password, server)
├── channel_listener.py    # Telethon client — monitors VIP channel
├── parser.py              # Claude SDK — parse signals into structured data
├── trade_manager.py       # Position lifecycle — open, trail SL, close
├── mt5_client.py          # MT5 operations — thin wrapper around MetaTrader5
├── state.json             # Active trade state (survives restarts)
└── logs/                  # Trade log and message history
    └── trades.log
```

## Message Classification

Claude parses each channel message into one of these types:

| Type | Example | Action |
|------|---------|--------|
| `NEW_SIGNAL` | "BUY NOW XAUUSD PRICE 4978 SL 4968 TP1 4980..." | Open trade |
| `SL_UPDATE` | "Edit your stop loss to TP1" / "Edit SL to 4985" | Modify SL |
| `TP_HIT` | "TP3 Hit!!!" | Log, optionally trail SL |
| `CLOSE_SIGNAL` | "Close all trades" / "Take profit now" | Close position |
| `NOISE` | "Congratulations everyone!!!" / "CPI news just hit" | Ignore |

### Parser Prompt (Claude)

```
You are a trading signal parser. Given a message from a VIP trading channel,
classify it and extract structured data.

Return JSON only:

For NEW_SIGNAL:
{"type": "NEW_SIGNAL", "direction": "BUY|SELL", "pair": "XAUUSD",
 "price": 4978, "sl": 4968, "tp": [4980, 4985, 4990, 4995, 5000, 5005, 5010, 5015]}

For SL_UPDATE:
{"type": "SL_UPDATE", "new_sl": 4980, "reason": "move to TP1"}

For TP_HIT:
{"type": "TP_HIT", "tp_number": 3}

For CLOSE_SIGNAL:
{"type": "CLOSE_SIGNAL", "reason": "provider says close"}

For NOISE:
{"type": "NOISE"}
```

## Lot Size & Partial Close Research

### MT5 XAUUSD Specs (MEXAtlantic-Real)

| Spec | Value |
|------|-------|
| Min lot | 0.01 |
| Max lot | 10.0 |
| Lot step | 0.01 |
| Contract size | 100 oz |
| Tick size | $0.01 |
| Tick value | $1.00 |
| Spread | ~$1.89 (189 points) |

### P&L at 0.01 lot
- **$1 per $1 price move** (0.01 lot = 1 oz gold)
- **$10 SL = $10 risk** (typical signal SL is $10)
- **Margin: ~$10** at 1:500 leverage
- **Account balance: $92.81**

### Can We Partial Close?

**NO.** At 0.01 lot (minimum lot = minimum step), you cannot partially close.
MT5 requires the close volume to be at least `volume_min` (0.01), and you can't
close 0.01 from a 0.01 position and keep the rest — there IS no rest.

### Partial Close With Larger Lots

If lot size increases in the future:

| Lot Size | Positions | Margin | Risk ($10 SL) | Partial Close? |
|----------|-----------|--------|----------------|----------------|
| 0.01 | 1 | $10 | $10 | NO |
| 0.02 | 1 | $20 | $20 | YES (close 0.01 at TP, keep 0.01) |
| 0.03 | 1 | $30 | $30 | YES (close at TP1, TP2, keep 0.01) |
| 0.08 | 1 | $80 | $80 | YES (close 0.01 at each of 8 TPs) |

**For now (0.01 lot):** Trail SL only, close entire position at final TP or when signaled.

**Future option:** Open multiple 0.01 lot positions (e.g., 3 positions), close each at different TPs.
But this multiplies risk — 3 positions = $30 risk on $92 account (32%). Only viable with more capital.

## Trade Lifecycle (0.01 Lot Strategy)

```
1. NEW_SIGNAL received
   ├── Parse: BUY XAUUSD @ 4978, SL 4968, TP1-TP8
   ├── Validate: market open? no active trade? pair = XAUUSD?
   └── Execute: mt5.order_send(BUY 0.01 XAUUSD, SL=4968)
       └── Save to state.json: {entry: 4978, sl: 4968, tp: [...], direction: BUY}

2. TP_HIT messages (informational)
   ├── Log which TP was hit
   └── (No action unless accompanied by SL_UPDATE)

3. SL_UPDATE received
   ├── Parse: "Edit SL to TP2 (4985)" → new_sl = 4985
   ├── Validate: new_sl makes sense (above entry for BUY, below for SELL)
   └── Execute: mt5.order_send(MODIFY position, SL=4985)
       └── Update state.json

4. Position hits SL or final TP
   ├── MT5 auto-closes the position
   ├── Detect via position check (polling every 30s)
   └── Clear state.json, log result

5. CLOSE_SIGNAL received
   ├── Close position at market
   └── Clear state.json, log result
```

## Safety Rules

1. **One trade at a time** — ignore new signals while a position is open
2. **XAUUSD only** — ignore signals for other pairs
3. **Max lot: 0.01** — hardcoded cap, cannot be exceeded by parser
4. **Market must be open** — don't attempt trades when market is closed (weekends, holidays)
5. **Validate SL direction** — BUY SL must be below entry, SELL SL must be above
6. **Stale signal protection** — if signal is older than 60 seconds when processed, skip
7. **MT5 must be connected** — if MT5 terminal is not running, log error and notify

## Notification to User

When a trade is executed/modified/closed, send a message to the Neo Telegram bot so you see it:

```
📊 Signal Trader: BUY XAUUSD @ 4978
   SL: 4968 | TP1: 4980 → TP8: 5015
   Lot: 0.01 | Risk: $10.00
```

This reuses the existing Neo notification pipeline — Signal Trader writes to
`C:/Openclaw/openclaw_lite/memory/workspace/inbox/` or calls Neo's send_message.

**Simpler option:** Signal Trader has its own Telegram bot (separate from Neo) that DMs you updates.
Or it could just send messages via the same Telethon client to your Saved Messages.

## Config

### config.json

```json
{
  "channel": {
    "id": -1002079334288,
    "name": "TRUE NORTH - VIP"
  },
  "trading": {
    "pair": "XAUUSD",
    "mt5_symbol": "XAUUSD..",
    "lot_size": 0.01,
    "max_lot": 0.01,
    "enabled": true
  },
  "safety": {
    "max_open_trades": 1,
    "stale_signal_seconds": 60,
    "position_poll_interval": 30
  },
  "notifications": {
    "method": "saved_messages",
    "enabled": true
  }
}
```

### .env

```
# Telegram User Client (from https://my.telegram.org)
TELEGRAM_API_ID=36903610
TELEGRAM_API_HASH=db4b8336186e0f4db8f89bc447aa7394

# MT5
MT5_LOGIN=930329
MT5_PASSWORD=****
MT5_SERVER=MEXAtlantic-Real
```

## Dependencies

```
telethon        # Telegram user client (listen to VIP channel)
MetaTrader5     # MT5 Python package (already installed)
claude-code-sdk # Signal parsing (already installed, free via Max)
python-dotenv   # Load .env
```

## Setup Steps

1. **Get Telegram API credentials:** DONE
   - `api_id`: 36903610
   - `api_hash`: saved in `.env`

2. **Find VIP channel ID:** PENDING
   - Run `python find_channel.py` in terminal (needs phone + OTP once)
   - Channel: "TRUE NORTH - VIP" (2,312 subscribers, gold-only signals)
   - Add numeric ID to `config.json`

3. **First run — Telethon auth:** PENDING
   - Run `find_channel.py` or `main.py` in terminal
   - Enter phone number + OTP code from Telegram
   - Creates `session.session` file (stays logged in after that)

4. **Start Signal Trader:**
   ```
   cd C:/Projects/signal-trader
   python main.py
   ```
   Or add to Neo's watchdog as a third subprocess.

## Integration with Watchdog (Optional)

Add Signal Trader as a third process in Neo's watchdog, alongside Neo and MarketMaster:

```json
// config.json (openclaw_lite)
{
  "signal_trader": {
    "enabled": true,
    "path": "C:/Projects/signal-trader",
    "script": "main.py"
  }
}
```

This way `/restart` keeps everything alive: Neo + MarketMaster + Signal Trader.

## Files to Create

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `main.py` | ~50 | Wire Telethon + trade manager, handle shutdown |
| `channel_listener.py` | ~60 | Telethon NewMessage handler, filter channel |
| `parser.py` | ~80 | Claude SDK call, prompt, JSON extraction |
| `trade_manager.py` | ~150 | Position lifecycle, state persistence |
| `mt5_client.py` | ~100 | Open/modify/close orders, position polling |
| `config.json` | ~20 | Configuration |
| `.env` | ~6 | Credentials |

**Total: ~470 lines of new code.**

## Edge Cases

- **Market closed:** Skip signal, log warning. Don't queue for market open (price will be stale).
- **MT5 disconnected:** Log error, send notification. Don't crash — reconnect on next signal.
- **Duplicate signal:** Parser returns NEW_SIGNAL but position already open → ignore.
- **Ambiguous message:** Claude returns NOISE if it can't confidently parse → safe default.
- **Signal provider edits message:** Telethon fires `MessageEdited` event → re-parse if needed.
- **Multiple signals rapid-fire:** Only first one executes (one trade at a time rule).
- **Bot restarts mid-trade:** `state.json` persists the active trade. On startup, check MT5 for open positions and reconcile with state.
- **SL_UPDATE with no active trade:** Ignore (position might have already been closed by SL).
- **Price moved far from signal:** If current price is >$5 from signal price, skip (market already moved).

## Risk Awareness

- **$10 risk per trade on $92 account = 10.8% risk** — aggressive but manageable for small account growth
- **Spread of ~$1.89** eats into the first TP ($2 target = only $0.11 net)
- **VIP signal quality** is the biggest variable — bot executes blindly what they say
- **No guarantee of fills** at exact prices — market orders fill at current price, not signal price
- **Weekend gaps** — if position is open over weekend, Monday open can gap past SL

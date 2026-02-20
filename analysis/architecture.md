# Technical Architecture Analysis

## 1. Current State Assessment

**Implemented:** Only `find_channel.py` (utility to discover channel IDs), `config.json`, `.env`, and `session.session`.

**Not implemented:** All 5 core modules -- `main.py`, `channel_listener.py`, `parser.py`, `trade_manager.py`, `mt5_client.py`. Zero lines of core business logic exist.

**Verdict:** This is a greenfield project with a well-documented plan. The plan is solid in its scope and covers most edge cases. The architecture below addresses the gaps.

---

## 2. Module Interface Design

### Data Structures (shared types -- define in `models.py`)

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time

class SignalType(str, Enum):
    NEW_SIGNAL = "NEW_SIGNAL"
    SL_UPDATE = "SL_UPDATE"
    TP_HIT = "TP_HIT"
    CLOSE_SIGNAL = "CLOSE_SIGNAL"
    NOISE = "NOISE"

class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

@dataclass
class ParsedSignal:
    type: SignalType
    raw_message: str
    timestamp: float  # message timestamp from Telegram
    # Only for NEW_SIGNAL:
    direction: Optional[Direction] = None
    pair: Optional[str] = None
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: list[float] = field(default_factory=list)
    # Only for SL_UPDATE:
    new_sl: Optional[float] = None
    reason: Optional[str] = None
    # Only for TP_HIT:
    tp_number: Optional[int] = None
    # Only for CLOSE_SIGNAL:
    close_reason: Optional[str] = None

@dataclass
class TradeState:
    """Persisted to state.json. Represents one active trade."""
    ticket: int               # MT5 position ticket
    direction: Direction
    pair: str
    entry_price: float        # actual fill price from MT5
    signal_price: float       # price from signal (for reference)
    current_sl: float
    tp_levels: list[float]
    lot_size: float
    opened_at: float          # unix timestamp
    last_updated: float       # unix timestamp
    tp_hits: list[int] = field(default_factory=list)  # which TPs have been hit

@dataclass
class TradeResult:
    success: bool
    ticket: Optional[int] = None  # MT5 position ticket
    error_code: Optional[int] = None
    error_message: Optional[str] = None
    fill_price: Optional[float] = None

@dataclass
class Config:
    channel_id: int
    channel_name: str
    pair: str
    mt5_symbol: str
    lot_size: float
    max_lot: float
    trading_enabled: bool
    max_open_trades: int
    stale_signal_seconds: int
    position_poll_interval: int
    notifications_enabled: bool
    notification_method: str
```

### Module Contracts

**channel_listener.py**
```python
# Exports:
async def start_listener(client: TelegramClient, channel_id: int,
                         on_message: Callable[[str, float], Awaitable[None]]) -> None:
    """Register handler for new messages from the VIP channel.
    on_message receives (message_text, message_timestamp).
    Also handles MessageEdited events."""

# Internal: Telethon event handler, channel ID filter
```

**parser.py**
```python
# Exports:
async def parse_signal(message: str, context: Optional[str] = None) -> ParsedSignal:
    """Send message to Claude, return structured ParsedSignal.
    context = recent message history for disambiguation.
    Raises ParserError on Claude API failure or invalid response."""
```

**trade_manager.py**
```python
# Exports:
class TradeManager:
    def __init__(self, config: Config, mt5: MT5Client):
        ...
    async def handle_signal(self, signal: ParsedSignal) -> None:
        """Main entry point. Routes signal to appropriate handler."""
    async def start_polling(self) -> None:
        """Start background position polling loop."""
    def load_state(self) -> Optional[TradeState]:
        """Load active trade from state.json."""
    def save_state(self, state: TradeState) -> None:
        """Persist trade state to state.json atomically."""
    def clear_state(self) -> None:
        """Remove state.json (trade closed)."""
    async def reconcile_on_startup(self) -> None:
        """Compare state.json with actual MT5 positions. Fix discrepancies."""
```

**mt5_client.py**
```python
# Exports:
class MT5Client:
    def __init__(self, login: int, password: str, server: str):
        ...
    def connect(self) -> bool:
        """Initialize and login to MT5. Returns success."""
    def disconnect(self) -> None:
        """Shut down MT5 connection."""
    def is_connected(self) -> bool:
        """Check MT5 terminal status."""
    def is_market_open(self, symbol: str) -> bool:
        """Check if symbol is tradeable right now."""
    def get_current_price(self, symbol: str) -> tuple[float, float]:
        """Returns (bid, ask) for symbol."""
    def open_position(self, symbol: str, direction: Direction, lot: float,
                      sl: float, tp: Optional[float] = None) -> TradeResult:
        """Send market order. Returns TradeResult with ticket."""
    def modify_sl(self, ticket: int, new_sl: float) -> TradeResult:
        """Modify stop loss on existing position."""
    def close_position(self, ticket: int) -> TradeResult:
        """Close position at market."""
    def get_position(self, ticket: int) -> Optional[dict]:
        """Get position info by ticket. None if closed."""
    def get_open_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Get all open positions, optionally filtered by symbol."""
```

**main.py**
```python
# Entry point:
async def main():
    """
    1. Load and validate config
    2. Initialize MT5Client, connect
    3. Initialize TradeManager
    4. Reconcile state on startup
    5. Start Telethon client
    6. Register channel listener with callback
    7. Start position polling
    8. Run until interrupted (Ctrl+C or SIGTERM)
    9. Graceful shutdown: disconnect MT5, disconnect Telegram
    """
```

---

## 3. State Management Strategy

### state.json Structure

```json
{
  "version": 1,
  "trade": {
    "ticket": 12345678,
    "direction": "BUY",
    "pair": "XAUUSD",
    "entry_price": 4978.50,
    "signal_price": 4978.00,
    "current_sl": 4968.00,
    "tp_levels": [4980, 4985, 4990, 4995, 5000, 5005, 5010, 5015],
    "lot_size": 0.01,
    "opened_at": 1708012345.678,
    "last_updated": 1708012400.123,
    "tp_hits": [1, 2]
  }
}
```

When no trade is active: file does not exist (or contains `{"version": 1, "trade": null}`).

### Read/Write Strategy

- **Read on startup:** Load state, reconcile with MT5.
- **Write after every state change:** trade opened, SL modified, TP hit logged, trade closed.
- **Atomic writes:** Write to `state.json.tmp`, then rename to `state.json`. This prevents corruption if process dies mid-write.
- **Clear on close:** Delete state.json when trade is closed/stopped.

### Crash Recovery

On startup, `reconcile_on_startup()` handles these cases:

| state.json | MT5 Position | Action |
|-----------|-------------|--------|
| Has trade | Position exists | Resume tracking (normal restart) |
| Has trade | Position gone | Trade was closed by SL/TP while bot was down. Clear state, log result. |
| No trade | Position exists | Orphaned position from manual trade or bug. Log warning, do NOT touch it. |
| No trade | No position | Clean slate. Ready for signals. |

---

## 4. Error Handling Strategy

| Failure Mode | Behavior | Recovery |
|-------------|----------|----------|
| MT5 disconnection during trade | `open_position` returns `TradeResult(success=False)` | Log error, notify user, do NOT retry automatically (price will have moved) |
| MT5 disconnection during SL modify | Modify returns failure | Log, retry once after 5s. If still fails, notify user. |
| Telegram connection drop | Telethon auto-reconnects (built-in) | Log reconnection. Messages sent while disconnected may be missed -- on reconnect, check last few messages. |
| Claude API failure | `parse_signal` raises `ParserError` | Log the raw message, notify user "manual review needed", do NOT attempt fallback parsing |
| Claude returns invalid JSON | JSON parse fails in `parse_signal` | Return `NOISE` as safe default. Log the raw Claude response for debugging. |
| MT5 order rejected | `TradeResult` has error code | Log rejection reason (e.g., insufficient margin, invalid SL). Notify user. Do NOT retry. |
| state.json corrupted | JSON parse fails on load | Log corruption. Treat as "no active trade." Check MT5 for positions to reconcile. |
| Invalid signal values | Validation fails in trade_manager | Log the invalid signal. Classify as NOISE. Notify user. |

### Critical Principle: Fail Safe

When in doubt, do nothing. Every error path should result in NO TRADE rather than a BAD TRADE. The cost of missing a signal is a missed opportunity. The cost of executing a bad trade is real money lost.

---

## 5. Async Architecture

### The Problem

Telethon is async (asyncio). MetaTrader5 Python package is sync-only (blocking calls that can take 100ms-2s). Running MT5 calls directly in the async event loop would block all Telegram message processing.

### Recommended Solution: `asyncio.run_in_executor`

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

class MT5Client:
    def __init__(self, ...):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")

    async def open_position_async(self, ...) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.open_position, ...)
```

**Why single thread:** MT5 Python API is NOT thread-safe. All MT5 calls must go through one thread. The ThreadPoolExecutor with `max_workers=1` ensures serial execution.

**Why not a separate process:** Overkill for this use case. The MT5 calls are infrequent (a few per day at most). `run_in_executor` is simple and sufficient.

### Event Loop Architecture

```
asyncio event loop (main thread)
  |-- Telethon client (async message handler)
  |-- Position polling task (asyncio.create_task, runs every 30s)
  |-- Graceful shutdown handler (SIGINT/SIGTERM)
  |
  `-- ThreadPoolExecutor (1 thread)
        `-- All MT5 blocking calls
```

---

## 6. Message Flow (Complete Trace)

```
1. Telegram server pushes message to Telethon client
2. channel_listener.py: on_new_message(event)
   a. Check: event.chat_id == config.channel_id? If not, ignore.
   b. Extract: text = event.message.message, ts = event.message.date.timestamp()
   c. Call: await on_message(text, ts)

3. main.py: on_message callback routes to trade_manager
   a. Call: signal = await parse_signal(text)
   b. Call: await trade_manager.handle_signal(signal)

4. trade_manager.py: handle_signal(signal)
   a. If signal.type == NOISE: return
   b. If signal.type == NEW_SIGNAL:
      i.   Check: is there an active trade? If yes, log and return.
      ii.  Check: signal.pair == config.pair? If not, log and return.
      iii. Check: is signal stale? (now - signal.timestamp > 60s) If yes, return.
      iv.  Check: mt5.is_market_open(config.mt5_symbol)? If not, return.
      v.   Check: price sanity (current price within $5 of signal price)? If not, return.
      vi.  Validate: SL direction correct for BUY/SELL.
      vii. Clamp: lot_size = min(config.lot_size, config.max_lot)
      viii. Execute: result = await mt5.open_position_async(...)
      ix.  If success: save_state(TradeState(...)), send notification.
      x.   If failure: log error, send notification.
   c. If signal.type == SL_UPDATE:
      i.   Check: is there an active trade? If not, ignore.
      ii.  Validate: new_sl direction makes sense.
      iii. Execute: result = await mt5.modify_sl_async(ticket, new_sl)
      iv.  If success: update state, save_state(), notify.
   d. If signal.type == TP_HIT:
      i.   Log which TP was hit.
      ii.  Update state.tp_hits if active trade.
   e. If signal.type == CLOSE_SIGNAL:
      i.   Check: is there an active trade? If not, ignore.
      ii.  Execute: result = await mt5.close_position_async(ticket)
      iii. Clear state, notify.

5. Position polling (every 30s):
   a. If active trade in state:
      i.  position = await mt5.get_position_async(ticket)
      ii. If position is None: trade was closed by SL/TP. Clear state, log, notify.
```

---

## 7. Configuration Validation

### At Startup (fail fast)

```python
def validate_config(config: Config) -> list[str]:
    errors = []
    if config.channel_id == 0:
        errors.append("channel.id is not set")
    if config.lot_size <= 0 or config.lot_size > config.max_lot:
        errors.append(f"lot_size {config.lot_size} invalid (must be 0 < lot <= {config.max_lot})")
    if config.max_lot > 0.1:  # Hard safety cap
        errors.append(f"max_lot {config.max_lot} exceeds safety cap of 0.1")
    if config.stale_signal_seconds < 10:
        errors.append("stale_signal_seconds too low (min 10)")
    if config.pair not in ["XAUUSD"]:  # Whitelist, not blacklist
        errors.append(f"pair '{config.pair}' not in allowed pairs")
    return errors
```

Also validate at startup:
- `.env` has all required variables (non-empty)
- MT5 credentials can connect
- Telegram session file exists (or prompt for auth)
- MT5 symbol exists and is tradeable

### At Runtime

- Every signal: validate parsed values against config bounds
- Every trade: re-check MT5 connection before sending order
- Every SL modify: validate new SL is sensible

---

## 8. Logging Strategy

### Levels

| Level | Usage |
|-------|-------|
| DEBUG | Raw Telegram messages, raw Claude responses, MT5 API call details |
| INFO | Signal parsed, trade executed, SL modified, trade closed, state saved |
| WARNING | Stale signal skipped, market closed, SL_UPDATE with no active trade |
| ERROR | MT5 connection failed, Claude API error, order rejected, state corruption |
| CRITICAL | Unhandled exception, MT5 login failure, configuration invalid |

### Format

```python
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/trades.log"),
        logging.StreamHandler()  # also print to console
    ]
)
```

### Audit Requirements

Every trade action MUST log:
- Timestamp
- Signal that triggered it (type, raw message text)
- Action taken (open/modify/close)
- MT5 request details (symbol, direction, lot, SL, TP)
- MT5 response (ticket, fill price, error code)
- State before and after

This creates an audit trail for debugging and post-trade analysis.

---

## 9. Recommendations

### Add a `models.py` file
The plan does not include a shared types module. All data classes should live in `models.py` to avoid circular imports and ensure type consistency.

### Add a `requirements.txt`
Pin dependency versions. Currently there is none.

### Add a `.gitignore`
Must exclude: `.env`, `session.session`, `state.json`, `logs/`, `__pycache__/`.

### Consider a dry-run mode
Add `"dry_run": true` to config.json. In this mode, everything runs normally except MT5 orders are logged but not executed. Essential for testing with real Telegram messages.

### MessageEdited handling
PLAN.md mentions it but does not spec the behavior. Recommendation: Only handle edits for messages less than 60 seconds old. Re-parse the edited message and apply it. If the original message was already acted on (trade opened), do NOT re-act on the edit -- just log it.

### Consider notifications via Telethon Saved Messages
This is the simplest approach. The Telethon client is already connected. Just send `await client.send_message("me", notification_text)`. No need for a separate bot or Neo integration for v1.

# VIP Signal Auto-Trader -- Design Document

## Project Status

**Current state: Pre-implementation.** Only a utility script (`find_channel.py`), configuration files (`config.json`, `.env`), and a Telethon session file exist. All 5 core modules remain to be built.

The plan (PLAN.md) is well-documented and covers approximately 90% of what is needed. This design document fills the remaining gaps and provides implementation-ready specifications.

---

## Architecture

### System Overview

```
VIP Telegram Channel
        |
        v
+--------------------+
|  Telethon Client   |  channel_listener.py -- async event handler
|  (asyncio)         |  Filters by channel_id, extracts text + timestamp
+--------+-----------+
         |
         v
+--------------------+
|  Signal Parser     |  parser.py -- Anthropic API call
|  (async HTTP)      |  Returns ParsedSignal dataclass
+--------+-----------+
         |
         v
+--------------------+
|  Trade Manager     |  trade_manager.py -- validation + lifecycle
|  (async)           |  State persistence, safety checks, routing
+--------+-----------+
         |
         v
+--------------------+
|  MT5 Client        |  mt5_client.py -- sync calls via run_in_executor
|  (ThreadPool 1)    |  Open/modify/close positions, polling
+--------------------+
```

### File Structure (Updated)

```
C:/Projects/SignalTrader/
+-- main.py                # Entry point: wire components, start event loop
+-- models.py              # Shared data classes and enums (NEW -- not in PLAN.md)
+-- channel_listener.py    # Telethon message handler
+-- parser.py              # Claude API signal parser
+-- trade_manager.py       # Position lifecycle and safety validation
+-- mt5_client.py          # MT5 wrapper with async bridge
+-- config.json            # Runtime configuration
+-- .env                   # Credentials (NEVER commit)
+-- .gitignore             # Protect credentials (MUST CREATE FIRST)
+-- requirements.txt       # Pinned dependencies (NEW -- not in PLAN.md)
+-- state.json             # Active trade state (created at runtime)
+-- STOP_TRADING           # Kill switch file (create to halt, delete to resume)
+-- logs/
|   +-- trades.log         # Audit log
+-- analysis/              # This design analysis (not deployed)
+-- PLAN.md
+-- DESIGN.md
+-- TEAM.md
```

**Change from PLAN.md:** Added `models.py` for shared types and `requirements.txt` for dependency pinning. Total estimated lines: ~530 (up from ~470).

---

## Tech Stack Decision: Claude SDK vs. Anthropic API

**PLAN.md specifies:** `claude-code-sdk` (Claude Code SDK)

**Recommendation: Use the `anthropic` Python package instead.**

Rationale:
- `claude-code-sdk` is designed for spawning agentic coding sessions -- overkill for text-in/JSON-out parsing
- The `anthropic` package provides a direct API call: simple, fast, well-documented
- Latency: direct API call (~1-2s) vs SDK subprocess spawn (~3-5s)
- The only concern is cost -- if Claude Max subscription provides free API access via the SDK but not via the `anthropic` package, then the SDK is justified

**Action required:** Verify whether the `anthropic` package works with Claude Max credentials (API key). If it does, use it. If only `claude-code-sdk` provides free access, keep the SDK but wrap it to minimize subprocess overhead.

**If using the Anthropic API:**
```
ANTHROPIC_API_KEY=sk-ant-...
```
Add to `.env`. Use `claude-sonnet-4-20250514` for low-latency, low-cost parsing.

---

## Data Model

### ParsedSignal

```python
@dataclass
class ParsedSignal:
    type: SignalType           # NEW_SIGNAL | SL_UPDATE | TP_HIT | CLOSE_SIGNAL | NOISE
    raw_message: str           # Original Telegram message text
    timestamp: float           # Telegram server timestamp (UTC)
    direction: Optional[Direction]  # BUY | SELL (NEW_SIGNAL only)
    pair: Optional[str]        # "XAUUSD" (NEW_SIGNAL only)
    price: Optional[float]     # Signal price (NEW_SIGNAL only)
    sl: Optional[float]        # Stop loss (NEW_SIGNAL only)
    tp: list[float]            # Take profit levels (NEW_SIGNAL only)
    new_sl: Optional[float]    # New SL value or TP reference (SL_UPDATE only)
    reason: Optional[str]      # Human-readable reason
    tp_number: Optional[int]   # Which TP was hit (TP_HIT only)
```

### TradeState (state.json)

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
    "last_updated": 1708012400.123
  }
}
```

No active trade: `{"version": 1, "trade": null}` or file does not exist.

---

## Safety Architecture (Defense in Depth)

Safety enforcement happens at THREE levels. Even if one level has a bug, the others catch it.

### Level 1: Parser Output Validation (trade_manager.py)

After Claude returns a ParsedSignal, validate all fields before acting:
- Direction is BUY or SELL (not null, not garbage)
- Pair matches config whitelist (exact match)
- Price is positive and within $10 of current market price
- SL is on correct side of price
- SL distance does not exceed $20 (configurable)
- At least 1 TP level exists
- Signal is not stale (< 60 seconds old)

### Level 2: Trade Execution Guards (mt5_client.py)

Before every order, the MT5 client itself enforces:
- Lot size does not exceed `ABSOLUTE_MAX_LOT` (hardcoded 0.05, configurable down to 0.01)
- MT5 terminal is connected (check before every call)
- Symbol exists and is tradeable
- Kill switch file does not exist

### Level 3: MT5 Server-Side Protection

MT5 itself rejects invalid orders:
- SL too close to price (trade_stops_level)
- Insufficient margin
- Market closed
- Invalid volume

### Safety Rules (Complete List)

| # | Rule | Enforcement |
|---|------|-------------|
| 1 | One trade at a time | trade_manager checks state.json AND mt5.get_open_positions() |
| 2 | XAUUSD only | Whitelist in config, checked in trade_manager |
| 3 | Max lot 0.01 | Config + hardcoded cap in mt5_client |
| 4 | Market must be open | Check mt5.symbol_info().trade_mode before order |
| 5 | SL direction valid | Validation pipeline in trade_manager |
| 6 | Stale signal (<60s) | Timestamp comparison in trade_manager |
| 7 | MT5 must be connected | Check mt5.terminal_info() before every order |
| 8 | Price sanity ($10 max deviation) | Validation pipeline in trade_manager |
| 9 | Max SL distance ($20) | Validation pipeline in trade_manager |
| 10 | Kill switch (STOP_TRADING file) | Checked in trade_manager before any action |
| 11 | Dry-run mode | Config flag, checked in mt5_client |

---

## Async Strategy

**Problem:** Telethon is async. MT5 Python package is sync-only (blocking).

**Solution:** `asyncio.run_in_executor` with a single-thread `ThreadPoolExecutor`.

```python
self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")

async def open_position_async(self, ...) -> TradeResult:
    return await asyncio.get_event_loop().run_in_executor(
        self._executor, self.open_position, ...
    )
```

**Why one thread:** MT5 Python API is not thread-safe. All calls must be serialized.

**Event loop layout:**
```
asyncio main loop
  +-- Telethon event handler (on_new_message)
  +-- Position polling task (every 30s)
  +-- Shutdown handler (SIGINT/SIGTERM)
  +-- MT5 calls offloaded to ThreadPoolExecutor(1)
```

---

## Error Handling Matrix

| Failure | Behavior | Recovery |
|---------|----------|----------|
| MT5 disconnection | Order returns failure | Log, notify user, do NOT retry (price moved) |
| Telegram drop | Telethon auto-reconnects | Messages during downtime are stale, auto-skipped |
| Claude API timeout | `parse_signal` raises exception | Classify as NOISE, log raw message, notify user |
| Claude returns bad JSON | JSON parse fails | Classify as NOISE, log raw response |
| MT5 order rejected | TradeResult has error code | Log reason, notify user, do NOT retry |
| state.json corrupted | JSON parse fails on load | Treat as no active trade, reconcile with MT5 |
| Process crash mid-trade | state.json may be stale | On restart, reconcile state with MT5 positions |

**Core principle: When in doubt, do nothing.** Missing a signal costs a missed opportunity. Executing a bad trade costs real money.

---

## State Management

### Atomic Writes

```python
def save_state(self, state: TradeState) -> None:
    data = {"version": 1, "trade": asdict(state)}
    tmp_path = "state.json.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, "state.json")  # atomic on most filesystems
```

### Startup Reconciliation

| state.json | MT5 Position | Action |
|-----------|-------------|--------|
| Has trade | Position exists | Resume tracking |
| Has trade | Position gone | Clear state, log (closed by SL/TP during downtime) |
| No trade | Position exists | Log warning, do NOT manage it (may be manual trade) |
| No trade | No position | Clean slate |

---

## Security Mitigations

### Immediate Actions (Before Any Code)

1. **Create `.gitignore`** with: `.env`, `*.session`, `state.json`, `logs/`, `__pycache__/`, `analysis/`
2. **Change MT5 password** -- the current one appears to be a default/temp password
3. **Verify Telegram 2FA** is enabled on the account

### Credential Strategy

`.env` + `python-dotenv` is appropriate for this single-developer, single-machine project. No need for vault, encrypted storage, or cloud secrets managers. The key protection is `.gitignore` and file system permissions.

### Key Vulnerability: Session File

`session.session` contains Telegram auth tokens. If copied, gives full account access. Mitigation: file permissions, never commit, awareness that this is the most sensitive file in the project.

---

## MVP Scope Definition

### In (Must Have for v1)

| Module | Key Features |
|--------|-------------|
| main.py | Config loading, validation, startup reconciliation, shutdown handler |
| models.py | All shared data classes and enums |
| channel_listener.py | NewMessage handler, channel filter, message extraction |
| parser.py | Claude API call, JSON extraction, schema validation |
| trade_manager.py | Full lifecycle: open, modify SL, close. State persistence. All safety rules. |
| mt5_client.py | Connect, open, modify, close, poll. Async bridge. Lot cap enforcement. |
| .gitignore | Protect credentials |
| requirements.txt | Pin dependencies |
| Dry-run mode | Test with real messages without risking money |
| Kill switch | File-based emergency stop |
| Notifications | Telethon Saved Messages for trade actions and errors |

### Out (Deferred to v2)

| Feature | Reason |
|---------|--------|
| MessageEdited handling | Rare case, adds complexity |
| Neo/Watchdog integration | Coupling risk, not needed for standalone operation |
| Daily loss tracking | Useful but not critical at $92 balance with 1-trade-at-a-time rule |
| Multiple pair support | Plan explicitly scopes to XAUUSD only |
| Partial close logic | Impossible at 0.01 lot |
| Account balance floor check | Nice-to-have, MT5 rejects on insufficient margin anyway |
| Time-of-day blackout windows | Signal provider's responsibility |

---

## Known Trade-Offs

### 1. Claude Parsing vs. Regex
**Chose: Claude.** Regex would be faster and cheaper but fragile against natural language variations in SL_UPDATE messages. Claude handles ambiguity and returns NOISE when uncertain. Trade-off: 1-2s latency per signal, external API dependency.

### 2. No Automatic Retry on Order Failure
**Chose: Fail and notify.** Retrying a market order 5 seconds later means accepting a different price. The user should decide. Trade-off: might miss a good trade if the failure was transient.

### 3. Do NOT Close Positions on Shutdown
**Chose: Leave positions open.** They have SL set, so they are protected by MT5 even without the bot. Closing on shutdown would crystallize unrealized P&L at potentially bad prices. Trade-off: position is unmanaged until bot restarts (no SL trailing).

### 4. One Thread for MT5
**Chose: ThreadPoolExecutor(1).** MT5 API is not thread-safe. Single thread ensures correctness. Trade-off: if MT5 call hangs, it blocks all subsequent MT5 operations. Mitigation: set timeouts on MT5 calls where possible.

### 5. Staleness Threshold at 60 Seconds
**Chose: 60 seconds.** Long enough to handle normal processing delays, short enough to avoid acting on stale prices. Trade-off: during high volatility, gold can move $5+ in 60 seconds. Could reduce to 30s but might reject valid signals that just took a moment to process.

---

## Open Questions

1. **Claude API access method:** Can the `anthropic` Python package be used with a Claude Max subscription, or is `claude-code-sdk` the only free option? This affects the parser implementation significantly.

2. **Channel ID verification:** The config has `channel_id: -1002079334288`. Has `find_channel.py` been run to confirm this is the correct ID? The setup steps mark this as PENDING.

3. **MT5 symbol name:** Config has `"mt5_symbol": "XAUUSD.."` (note the double dots). Is this correct for the MEXAtlantic-Real server? Wrong symbol name = all orders fail.

4. **Telethon session validity:** The `session.session` file exists (28KB). Has it been tested recently? Telegram sessions can expire after inactivity.

5. **Notifications to Saved Messages:** Will Telethon Saved Messages work while the bot is listening to a channel? Should be fine (same client, different operations) but needs testing.

---

## Implementation Order

Build in this order to enable testing at each stage:

```
Phase 1: Foundation (test: imports, config loading)
  1. .gitignore
  2. requirements.txt
  3. models.py (data classes)
  4. Config loading + validation in main.py (partial)

Phase 2: MT5 Integration (test: connect, get price, open/close in dry-run)
  5. mt5_client.py (sync methods)
  6. mt5_client.py (async bridge)

Phase 3: Signal Parsing (test: parse real channel messages offline)
  7. parser.py (Claude API call + JSON validation)

Phase 4: Trade Logic (test: handle signals in dry-run mode)
  8. trade_manager.py (validation pipeline)
  9. trade_manager.py (state management)
  10. trade_manager.py (full lifecycle)

Phase 5: Integration (test: end-to-end with real messages, dry-run)
  11. channel_listener.py
  12. main.py (complete wiring)
  13. Notifications (Saved Messages)
  14. Kill switch

Phase 6: Go-Live Prep
  15. Run in dry-run for 2+ trading sessions
  16. Review all logs for misparsed signals
  17. Execute go-live checklist (see below)
```

---

## Go-Live Checklist

Before setting `trading.enabled: true`:

- [ ] .gitignore is in place and verified
- [ ] MT5 password has been changed from the default
- [ ] Telegram 2FA is enabled
- [ ] Bot ran in dry-run mode for 2+ trading sessions (8+ hours)
- [ ] Zero misparsed signals during dry-run
- [ ] Startup reconciliation tested (kill bot while trade open, restart)
- [ ] Kill switch tested (create/delete STOP_TRADING file)
- [ ] Notifications to Saved Messages work
- [ ] Config verified: lot_size=0.01, max_lot=0.01, pair=XAUUSD
- [ ] MT5 symbol name verified (XAUUSD.. is correct for broker)
- [ ] Stale signal rejection tested
- [ ] Duplicate signal rejection tested
- [ ] SL validation tested (wrong-side SL rejected)
- [ ] Claude parse failure handled gracefully
- [ ] MT5 disconnection handled gracefully
- [ ] First live trade: watch in real-time, verify all parameters

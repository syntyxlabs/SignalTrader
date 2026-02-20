# Security and Safety Audit

## Executive Summary

This project handles **real money on a live MT5 account** and has access to a **personal Telegram account**. The attack surface is significant for a single-developer project. The most critical findings are:

1. **CRITICAL:** MT5 password stored in plaintext in `.env` -- and the actual password is visible in the PLAN.md
2. **CRITICAL:** No `.gitignore` exists -- session file and credentials at risk of being committed
3. **HIGH:** No financial safety nets beyond the 7 basic safety rules
4. **HIGH:** No input validation layer between Claude's output and trade execution
5. **MEDIUM:** No kill switch mechanism to halt trading immediately

---

## 1. Credential Handling Audit

### Finding 1.1: MT5 Password in Plaintext (.env)

**Severity:** CRITICAL

The `.env` file contains:
```
MT5_PASSWORD=Pwd4temp123!
```

This is a real trading account password. Additionally, the actual password value appears in `PLAN.md` in the `.env` example section (line 238 shows `****` but the actual .env file has the real password).

**Risks:**
- Anyone with file system access can read the password
- If this project is ever pushed to git (even accidentally), the password is in history forever
- The password follows a predictable pattern ("Pwd4temp123!") suggesting it may be a default or weak password

**Mitigations:**
1. **Immediate:** Create `.gitignore` before any git operations. Include `.env`, `session.session`, `state.json`, `logs/`, `*.session`.
2. **Immediate:** Remove the actual password from PLAN.md if it was ever written there.
3. **Short-term:** Change the MT5 password to something stronger and randomly generated.
4. **Appropriate for this project:** `.env` with dotenv is acceptable for a single-developer, single-machine project. Do NOT over-engineer with vault solutions. Just protect the file.

### Finding 1.2: Telegram Session File (session.session)

**Severity:** HIGH

The `session.session` file (28KB, SQLite database) contains Telegram authentication tokens. Anyone who copies this file can impersonate the user's Telegram account without needing the phone number or OTP.

**Risks:**
- Full access to all Telegram chats, contacts, and groups
- Could send messages as the user
- Could read VIP channel signals and front-run trades
- Could be used to manipulate the bot's behavior by posting fake messages

**Mitigations:**
1. **Immediate:** Add `*.session` to `.gitignore`
2. **Immediate:** Ensure file permissions are user-only: `chmod 600 session.session` (on the Windows equivalent, restrict to current user)
3. **Monitor:** Check Telegram "Active Sessions" periodically for unauthorized access

### Finding 1.3: Telegram API Hash

**Severity:** MEDIUM

The API hash (`db4b8336186e0f4db8f89bc447aa7394`) is in `.env`. If leaked alongside the API ID, someone could create their own Telegram client application under this user's API credentials.

**Mitigation:** Keep in `.env`, ensure `.gitignore` protects it. This is acceptable risk for this project.

### Finding 1.4: No .gitignore Exists

**Severity:** CRITICAL

There is currently NO `.gitignore` file. If the user runs `git init && git add .`, ALL credentials, session files, and state will be committed.

**Mitigation (immediate):** Create `.gitignore`:
```
.env
*.session
state.json
logs/
__pycache__/
*.pyc
analysis/
```

---

## 2. Trade Execution Safety Analysis

### Evaluating the 7 Safety Rules

**Rule 1: One trade at a time**
- **Assessment:** Good rule. Essential at 0.01 lot / $92 account.
- **Gap:** Must check BOTH state.json AND MT5 actual positions. If only checking state.json, a manually opened trade would not be detected.
- **Mitigation:** On every NEW_SIGNAL, check `mt5.get_open_positions("XAUUSD")` in addition to state.json.

**Rule 2: XAUUSD only**
- **Assessment:** Good. Prevents the parser from opening trades on unknown instruments.
- **Gap:** The check should be a whitelist, not a string comparison. Validate `signal.pair.upper().strip() == config.pair`.
- **Additional:** Also validate the MT5 symbol exists: `mt5.symbol_info(config.mt5_symbol)` at startup.

**Rule 3: Max lot 0.01**
- **Assessment:** Good. Hardcoded cap is the right approach.
- **Gap:** The cap should be enforced in `mt5_client.py` at the lowest level, not just in trade_manager. Defense in depth: even if trade_manager has a bug, mt5_client should refuse to send an order above `max_lot`.
- **Mitigation:** Add `assert lot <= self.max_lot` in `mt5_client.open_position()`.

**Rule 4: Market must be open**
- **Assessment:** Good. Prevents orders that would fail anyway.
- **Gap:** MT5's `symbol_info_tick()` can tell you if the market is in a session. Use `mt5.symbol_info(symbol).trade_mode` to check if trading is allowed.

**Rule 5: Validate SL direction**
- **Assessment:** Essential. BUY SL must be below current price, SELL SL must be above.
- **Gap:** Should validate against CURRENT price, not signal price. By the time the order executes, price may have moved.
- **Additional:** SL must be at least `symbol.trade_stops_level` points away from current price (MT5 enforces this, but checking first prevents unnecessary rejections).

**Rule 6: Stale signal protection (60s)**
- **Assessment:** Good. Prevents acting on old messages after a restart.
- **Gap:** 60 seconds may be too generous for gold. Gold can move $5 in 60 seconds during high volatility. Consider reducing to 30 seconds or making it configurable.
- **Additional:** The staleness check uses `time.time() - message_timestamp`. Ensure the message timestamp comes from Telegram's server time, not local clock.

**Rule 7: MT5 must be connected**
- **Assessment:** Good. Prevents blind order attempts.
- **Gap:** Check connection IMMEDIATELY before sending order, not just at startup. MT5 can disconnect at any time.
- **Mitigation:** `mt5_client.open_position()` should call `mt5.terminal_info()` to verify connection before every order.

---

## 3. Input Validation Deep Dive

### The Core Risk

Claude receives free-text Telegram messages and outputs JSON that directly controls trade execution. If Claude hallucinates, misparses, or returns unexpected data, real money is at risk.

### Validation Pipeline (MUST be implemented)

Every `ParsedSignal` with type `NEW_SIGNAL` MUST pass ALL of these checks before execution:

```python
def validate_new_signal(signal: ParsedSignal, config: Config,
                        current_bid: float, current_ask: float) -> tuple[bool, str]:
    """Returns (is_valid, rejection_reason)."""

    # 1. Direction must be BUY or SELL (not None, not garbage)
    if signal.direction not in (Direction.BUY, Direction.SELL):
        return False, f"Invalid direction: {signal.direction}"

    # 2. Pair must match config (exact match, case-insensitive)
    if signal.pair.upper().strip() != config.pair.upper():
        return False, f"Pair mismatch: {signal.pair} != {config.pair}"

    # 3. Price must be a positive number in reasonable range
    if not signal.price or signal.price <= 0:
        return False, f"Invalid price: {signal.price}"

    # 4. Price sanity: parsed price must be within $10 of current market
    current_price = current_ask if signal.direction == Direction.BUY else current_bid
    price_diff = abs(signal.price - current_price)
    if price_diff > 10.0:
        return False, f"Price {signal.price} too far from market {current_price} (diff: ${price_diff})"

    # 5. SL must exist and be positive
    if not signal.sl or signal.sl <= 0:
        return False, f"Invalid SL: {signal.sl}"

    # 6. SL direction check
    if signal.direction == Direction.BUY and signal.sl >= current_price:
        return False, f"BUY SL {signal.sl} >= price {current_price}"
    if signal.direction == Direction.SELL and signal.sl <= current_price:
        return False, f"SELL SL {signal.sl} <= price {current_price}"

    # 7. SL distance: max $20 risk (circuit breaker)
    sl_distance = abs(current_price - signal.sl)
    if sl_distance > 20.0:
        return False, f"SL distance ${sl_distance} exceeds $20 max"

    # 8. Must have at least 1 TP level
    if not signal.tp or len(signal.tp) == 0:
        return False, "No TP levels"

    # 9. TP direction check
    for tp in signal.tp:
        if signal.direction == Direction.BUY and tp <= signal.sl:
            return False, f"BUY TP {tp} <= SL {signal.sl}"
        if signal.direction == Direction.SELL and tp >= signal.sl:
            return False, f"SELL TP {tp} >= SL {signal.sl}"

    return True, ""
```

### SL_UPDATE Validation

```python
def validate_sl_update(signal: ParsedSignal, state: TradeState,
                       current_price: float) -> tuple[bool, str]:
    if not signal.new_sl or signal.new_sl <= 0:
        return False, f"Invalid new SL: {signal.new_sl}"

    # New SL must still be on the correct side
    if state.direction == Direction.BUY and signal.new_sl >= current_price:
        return False, f"BUY new SL {signal.new_sl} >= price {current_price}"
    if state.direction == Direction.SELL and signal.new_sl <= current_price:
        return False, f"SELL new SL {signal.new_sl} <= price {current_price}"

    # New SL should generally be better (closer to price) for trailing
    # But allow moves in either direction -- provider may have reasons
    return True, ""
```

---

## 4. Financial Safety Nets (Missing from Plan)

### 4.1 Maximum Daily Loss Limit

**Not in plan. Should be added.**

```python
# In config.json:
"safety": {
    "max_daily_loss_usd": 20.0,   # Stop trading if daily loss exceeds $20
    "max_daily_trades": 5          # Stop after 5 trades per day (win or lose)
}
```

Track daily P&L in state or a separate `daily_stats.json`. Reset at midnight. If daily loss exceeds threshold, refuse new trades until next day.

### 4.2 Kill Switch

**Not in plan. Should be added.**

Add a `"trading.enabled": true` flag in config.json (already exists). But also add a file-based kill switch:

```python
KILL_SWITCH_FILE = "STOP_TRADING"

def is_kill_switch_active() -> bool:
    return os.path.exists(KILL_SWITCH_FILE)
```

If the file `STOP_TRADING` exists in the project directory, refuse all trades. This allows emergency shutdown without restarting the bot -- just create the file.

### 4.3 Account Balance Check

Before opening any trade, verify the account has sufficient margin:

```python
account_info = mt5.account_info()
if account_info.margin_free < required_margin * 2:  # 2x safety factor
    return False, "Insufficient free margin"
```

### 4.4 Maximum SL Distance

Already covered in validation pipeline above ($20 max). This prevents a parsing error from setting SL $1000 away.

### 4.5 Position Size Validation (Defense in Depth)

In `mt5_client.py`, hardcode an absolute maximum:

```python
ABSOLUTE_MAX_LOT = 0.05  # Even if config says otherwise, never exceed this

def open_position(self, ..., lot: float, ...) -> TradeResult:
    if lot > ABSOLUTE_MAX_LOT:
        raise ValueError(f"Lot {lot} exceeds absolute max {ABSOLUTE_MAX_LOT}")
```

---

## 5. Attack Surface Analysis

### 5.1 Telegram Account Compromise

**Risk:** If the Telegram account is compromised, an attacker could:
- Read all VIP signals and front-run trades
- Send fake messages to the VIP channel (if they have send permission) to trigger trades
- Access the session file to create additional clients

**Mitigation:**
- Enable 2FA on the Telegram account
- The bot only listens to ONE specific channel ID -- messages from other sources are ignored
- The bot does not act on messages sent BY the user, only from the channel

**Residual risk:** If the VIP channel itself is compromised, the bot will execute whatever signals it posts. This is inherent to the architecture -- the bot trusts the channel.

### 5.2 Session File Stolen

**Risk:** Full Telegram account access without OTP.
**Mitigation:** File permissions, .gitignore, not sharing the machine.

### 5.3 Machine Access

**Risk:** If the machine is compromised, attacker has:
- All credentials (.env)
- Active MT5 session (can trade directly)
- Telegram session
- Ability to modify bot code to redirect trades

**Mitigation:** This is a local-machine bot. If the machine is compromised, all bets are off regardless. Standard OS security applies. Full disk encryption recommended.

### 5.4 VIP Channel Compromise

**Risk:** If the signal provider's channel is hacked, the bot would execute malicious signals.
**Mitigation:** The validation pipeline (Section 3) limits what can be executed. Max lot, max SL distance, price sanity checks, and the kill switch all limit damage. The $20 max daily loss limit provides a hard cap.

---

## 6. Dependency Security

### Telethon

- **Risk:** Third-party Telegram client. Handles authentication tokens.
- **History:** Well-maintained, widely used (10k+ GitHub stars). No major vulnerabilities in recent years.
- **Recommendation:** Pin to specific version. Monitor for updates.

### MetaTrader5

- **Risk:** Official MetaTrader Python package by MetaQuotes.
- **History:** Closed-source binary component. Limited audit capability.
- **Recommendation:** Only install from PyPI. Pin version. This is the only option for MT5 Python integration.

### claude-code-sdk (Claude Code SDK)

- **Risk:** Anthropic's SDK. Used to parse signals.
- **Note:** The plan says "free via Max subscription" using claude-code-sdk. This spawns a Claude Code subprocess. Verify this is the intended approach vs. using the Anthropic API directly. The Anthropic Python API (`anthropic` package) might be more appropriate for simple text-in, JSON-out parsing.
- **Recommendation:** Consider using the `anthropic` Python package directly instead of claude-code-sdk, as the SDK is designed for agentic coding tasks, not simple API calls.

### python-dotenv

- **Risk:** Very simple, widely used, minimal attack surface.
- **Recommendation:** Pin version. Low risk.

### Version Pinning

Create `requirements.txt`:
```
telethon==1.36.0
MetaTrader5==5.0.4424
anthropic==0.42.0
python-dotenv==1.0.1
```

---

## 7. Operational Security

### Monitoring

- The bot should log every action to `logs/trades.log`
- Send Telegram notifications (to Saved Messages) for every trade action
- Log heartbeat every 5 minutes ("bot alive, no active trade" or "bot alive, tracking XAUUSD BUY ticket 12345")

### Alerts (via Telegram notification)

- Trade opened
- Trade closed (and P&L)
- SL modified
- Error: MT5 disconnected
- Error: Claude API failed
- Warning: Signal rejected (with reason)
- Daily summary at end of trading day

### Safe Shutdown

```python
async def shutdown(signum, frame):
    logging.info("Shutdown signal received")
    # 1. Stop accepting new signals
    # 2. Do NOT close open positions (user can manage manually)
    # 3. Save current state
    # 4. Disconnect MT5
    # 5. Disconnect Telegram
    # 6. Exit cleanly
```

**Key decision:** On shutdown, do NOT auto-close positions. The position has a stop loss set by MT5, so it is protected even without the bot running. Closing on shutdown would crystalize losses unnecessarily.

### Restart Safety

If the bot crashes during `mt5.order_send()`:
- The order may or may not have been executed
- On restart, `reconcile_on_startup()` checks MT5 for the actual position
- If position exists but state.json is empty, log a warning (orphaned position)
- If state.json has a trade but MT5 does not, clear the stale state

This is the most dangerous edge case. The reconciliation logic must be tested thoroughly.

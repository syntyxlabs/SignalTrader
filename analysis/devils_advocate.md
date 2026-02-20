# Devil's Advocate Review

## 1. Scope Creep -- What to Cut from MVP

The PLAN.md is well-scoped for ~470 lines, but there are features that can and should be deferred.

### CUT from MVP

| Feature | Reason to Cut | Alternative for v1 |
|---------|--------------|-------------------|
| MessageEdited handling | Adds complexity for a rare case. Most signals are posted once. | Log edited messages, do not re-parse. Add in v2. |
| Notification via Neo pipeline | Coupling two projects adds integration risk. | Use Telethon Saved Messages -- already have the client. |
| Watchdog integration | Nice-to-have, not needed for first run. | Run `main.py` manually or with a simple batch script. |
| Multiple TP tracking in state | At 0.01 lot, TP tracking is purely informational. | Log TP hits, do not track them in state. |
| Daily loss limit | Good safety feature, but adds state tracking complexity. | For v1 with $92 account, the max_open_trades=1 rule limits exposure enough. Add in v2. |

### KEEP in MVP (non-negotiable)

| Feature | Why |
|---------|-----|
| State persistence (state.json) | Without this, a restart loses track of open positions. |
| Startup reconciliation | Without this, stale state after restart could cause the bot to think a position exists when it does not (or vice versa). |
| All 7 safety rules | Every one prevents real money loss. |
| Input validation pipeline | Parsing errors are the highest-probability failure mode. |
| Kill switch (file-based) | Dead simple to implement (3 lines of code) and provides emergency stop. |
| Dry-run mode | Must test with real messages before risking real money. |

---

## 2. Architecture Challenges

### Challenge: Claude SDK vs. Regex for Parsing

The plan uses `claude-code-sdk` for signal parsing. This is worth questioning.

**Arguments for Claude:**
- Signal messages vary in format ("BUY NOW XAUUSD" vs "XAUUSD BUY" vs "Gold buy signal")
- SL_UPDATE messages are particularly varied ("edit SL to TP1", "move stop to breakeven", "trail your stop to 4985")
- Claude handles ambiguity well -- returns NOISE when uncertain

**Arguments for regex:**
- Zero latency (Claude calls add 1-3 seconds)
- Zero cost (even if Claude Max is "free," it has rate limits)
- Zero external dependency (no API failures)
- If this ONE channel uses a consistent format, regex might work fine

**Verdict:** Claude is the right choice. The variety of message formats (NEW_SIGNAL, SL_UPDATE, TP_HIT, CLOSE_SIGNAL, NOISE) and the natural language nature of SL updates ("edit your stop loss to TP1" where "TP1" needs to be resolved to a price) makes regex fragile. However:

**Important concern about the SDK choice:** The plan specifies `claude-code-sdk`, which is designed for spawning Claude Code sessions for agentic coding tasks. For simple text-in/JSON-out parsing, the `anthropic` Python package is the correct choice. It is faster, simpler, and designed for exactly this use case:

```python
import anthropic

client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY from env

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=256,
    messages=[{"role": "user", "content": f"{SYSTEM_PROMPT}\n\nMessage: {message}"}]
)
```

If the user has a Claude Max subscription and wants to avoid API costs, they would need to verify whether the `anthropic` package works with Max credentials or if `claude-code-sdk` is indeed the only free option. This needs clarification before implementation.

### Challenge: Async Complexity

The architecture requires asyncio + ThreadPoolExecutor for MT5. This is correct but adds complexity to a ~470-line project.

**Simpler alternative considered:** Run everything synchronously. Telethon also supports synchronous usage via `client.run_until_disconnected()` with sync event handlers. MT5 calls would block the event loop for only 100-200ms per call, which is acceptable since signals come at most a few times per day.

**Verdict:** Keep the async approach. Telethon's strength is its async event-driven model. Even though signals are rare, the polling loop (every 30s) would interact poorly with synchronous blocking. The `run_in_executor` pattern is only ~5 extra lines of code.

---

## 3. Realistic Failure Modes (Top 5)

These are the failures most likely to happen in production, ordered by probability:

### Failure 1: Claude Returns Unexpected JSON (HIGH PROBABILITY)

Claude may return JSON with different field names, extra fields, or slightly different structure than expected. For example: `{"type": "NEW_SIGNAL", "action": "BUY"}` instead of `{"type": "NEW_SIGNAL", "direction": "BUY"}`.

**Mitigation:** Use strict JSON schema validation after parsing. If any required field is missing or wrong type, classify as NOISE. Test the prompt extensively with real messages from the channel BEFORE going live.

### Failure 2: MT5 Terminal Not Running / Crashed (HIGH PROBABILITY)

The MetaTrader5 Python package requires the MT5 terminal application to be running on the same machine. If MT5 closes (crash, update, accidental close), all trading calls fail silently or with error codes.

**Mitigation:** Check `mt5.terminal_info()` before every trade operation. If MT5 is down, send a Telegram notification immediately. Consider a separate watchdog that monitors the MT5 process.

### Failure 3: Signal Arrives While Bot is Restarting (MEDIUM PROBABILITY)

If the bot restarts (crash, manual restart, OS reboot), Telegram messages sent during downtime may or may not be delivered when the client reconnects. Telethon fetches recent messages on reconnect, but the message timestamps will be stale.

**Mitigation:** The 60-second staleness check handles this correctly. Messages received during downtime will be older than 60s and will be skipped. This is the CORRECT behavior -- prices will have moved.

### Failure 4: SL_UPDATE References "TP1" But We Need the Price (MEDIUM PROBABILITY)

The signal provider says "move SL to TP1" but does not give a numeric price. Claude needs to know the TP levels from the original signal to resolve "TP1" to a price (e.g., 4980).

**Mitigation:** The parser prompt should include the current trade context (TP levels) when parsing SL_UPDATE messages. Either:
- Pass the original signal's TP levels as context to Claude
- Or resolve "TP1" to a price in trade_manager using stored state

The second approach is more reliable. If the signal says "move SL to TP1" and Claude returns `{"type": "SL_UPDATE", "new_sl": "TP1"}`, trade_manager can resolve TP1 from `state.tp_levels[0]`.

### Failure 5: Order Rejected Due to Spread/Price Movement (MEDIUM PROBABILITY)

XAUUSD spread is ~$1.89 (189 points). During news events, spread can widen to $5+. If the signal says SL is $10 from entry but spread widens, the effective risk increases. Also, `TRADE_RETCODE_INVALID_STOPS` if SL is too close to current price.

**Mitigation:** Check `symbol_info.trade_stops_level` and ensure SL is at least that many points away. If SL would be invalid, log and skip (do not adjust SL silently).

---

## 4. Safety Rules Gaps

### Missing Rule: Maximum SL Distance

The plan has no cap on SL distance. If Claude parses an SL of $50 away from entry, that is $50 risk on a $92 account. Add: `max_sl_distance_usd: 20` in config.

### Missing Rule: Price Sanity Check

The plan mentions "$5 from signal price" in edge cases but it is not in the safety rules. It should be a formal safety rule with a configurable threshold.

### Missing Rule: Account Balance Floor

Do not trade if account balance drops below a threshold (e.g., $50). This prevents the bot from risking the last dollars.

### Missing Rule: Time-of-Day Filter (Optional)

Gold volatility spikes during news events (NFP, FOMC, CPI). Some signal providers avoid these times, but if the bot is running 24/5, it will execute signals during high-volatility periods. Consider an optional blackout window config.

---

## 5. Cost/Benefit of Features

| Feature | Essential? | Effort | Verdict |
|---------|-----------|--------|---------|
| State persistence | YES | Medium | Include -- restart safety |
| Position polling | YES | Low | Include -- detect SL/TP closure |
| Notifications | YES | Low | Include -- user needs to know what happened |
| Kill switch | YES | Trivial | Include -- 3 lines of code |
| Dry-run mode | YES | Low | Include -- required for testing |
| Startup reconciliation | YES | Medium | Include -- prevents ghost state |
| Config validation | YES | Low | Include -- fail fast on bad config |
| MessageEdited handling | NO | Medium | Defer to v2 |
| Daily loss tracking | NO | Medium | Defer to v2 |
| Multiple pair support | NO | High | Defer to v2+ |
| Partial close | NO | N/A | Impossible at 0.01 lot |

---

## 6. Critical Test Cases (Pre-Launch Checklist)

These tests MUST pass before the bot touches real money.

### Test 1: Dry-Run with Real Messages
- Enable dry-run mode
- Let the bot run for at least 1 full trading session (4-8 hours)
- Verify every signal is parsed correctly
- Verify every action that WOULD have been taken is correct
- Check: would the bot have lost money on any misparse?

### Test 2: New Signal Happy Path
- Send a test message matching the VIP channel format
- Verify: parser extracts correct direction, pair, price, SL, TPs
- Verify: all validation checks pass
- Verify (dry-run): MT5 order would have been sent with correct parameters

### Test 3: SL_UPDATE with "TP1" Reference
- Open a trade (dry-run)
- Send "Edit your stop loss to TP1"
- Verify: the parser and trade_manager correctly resolve TP1 to the stored price
- Verify: MT5 modify order would have correct new SL

### Test 4: Stale Signal Rejection
- Send a signal, wait 65 seconds, then process it
- Verify: signal is rejected as stale

### Test 5: Duplicate Signal Rejection
- Open a trade (dry-run)
- Send another NEW_SIGNAL
- Verify: second signal is rejected because a trade is already open

### Test 6: Kill Switch
- Create `STOP_TRADING` file
- Send a NEW_SIGNAL
- Verify: signal is rejected
- Delete the file, send again
- Verify: signal is processed

### Test 7: Crash Recovery
- Open a trade (or mock one in state.json)
- Kill the bot process
- Restart the bot
- Verify: state is loaded, reconciled with MT5, and polling resumes

### Test 8: Claude Parse Failure
- Mock Claude returning garbage / timeout
- Verify: bot does not crash, signal is classified as NOISE, error is logged

### Test 9: MT5 Disconnection
- Disconnect MT5 terminal
- Send a signal
- Verify: bot detects MT5 is down, logs error, sends notification, does not crash

### Test 10: Validation Rejection
- Send a signal with SL on wrong side (BUY with SL above price)
- Verify: rejected with clear log message

---

## 7. Go-Live Checklist

Before flipping `trading.enabled: true` and removing dry-run mode:

- [ ] All 10 test cases above pass
- [ ] Bot has run in dry-run mode for at least 2 full trading sessions with real VIP messages
- [ ] Zero misparsed signals during dry-run
- [ ] .gitignore is in place
- [ ] MT5 password has been changed from the default
- [ ] Kill switch mechanism works
- [ ] Notifications to Saved Messages work
- [ ] state.json atomic write is implemented
- [ ] Startup reconciliation is tested (kill bot while trade is open, restart)
- [ ] Logs are readable and contain all necessary audit information
- [ ] User has manually verified: lot_size=0.01, max_lot=0.01, pair=XAUUSD in config
- [ ] First live trade: watch it happen in real-time, verify everything matches

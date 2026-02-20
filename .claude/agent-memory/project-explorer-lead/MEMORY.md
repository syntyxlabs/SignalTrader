# Project Explorer Lead -- Memory

## Project: SignalTrader
- **Type:** Python automation bot (Telegram -> Claude parser -> MT5 trade executor)
- **Location:** C:\Projects\SignalTrader
- **State as of 2026-02-16:** Pre-implementation. Only find_channel.py, config.json, .env, session.session exist.
- **Plan:** PLAN.md (comprehensive, ~320 lines)
- **Design:** DESIGN.md (produced by this analysis)

## Key Findings
- Claude CLI sub-agents cannot be spawned from within a Claude Code session (nesting restriction). Perform all analyses directly.
- The project handles real money ($92.81 MT5 account). Security and safety validation are paramount.
- MT5 Python API is sync-only; must use asyncio.run_in_executor with single-thread pool.
- The plan specifies claude-code-sdk but anthropic Python package is more appropriate for simple parsing.
- MT5 symbol is "XAUUSD.." (double dots) -- needs verification with broker.

## Critical Files
- `PLAN.md` -- full project plan
- `DESIGN.md` -- architecture and implementation guide
- `.env` -- contains real MT5 password in plaintext
- `session.session` -- Telegram auth tokens (28KB SQLite)
- `config.json` -- channel ID, trading params, safety limits

## Analysis Output
- `analysis/architecture.md` -- module interfaces, data flow, async strategy
- `analysis/security.md` -- credential audit, safety rules, validation pipeline
- `analysis/devils_advocate.md` -- scope cuts, failure modes, go-live checklist

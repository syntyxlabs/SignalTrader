# SignalTrader Memory

## 2026-03-10
- MT5 account: 935567 on MEXAtlantic-Real, leverage 1:500
- Balance: $800 (was $650→$700→$800 during session)
- Config changed: lot_size and max_lot reduced from 0.08 → 0.05
- **Missed trade on 2026-03-09**: TRUE NORTH VIP sent "BUY NOW XAUUSD" at 22:28, hit TP1-TP4. Bot missed everything because Claude SDK kept failing with `exit code 1`. All signals treated as noise.
- **Fix applied**: Added retry logic (3 attempts, 1s delay) + regex fallback parser to `parser.py`. Fallback handles: BUY/SELL NOW, BUY/SELL LIMIT, full signals with price/SL/TPs, TP hits, close signals, SL to breakeven, bare BUY/SELL. Bot restarted to pick up changes.
- Parser uses Claude Haiku via `claude_code_sdk` for signal classification
- SL strategy confirmed: TP1→no change, TP2→BE, TP3→TP1, TP4→TP2
- Channels reduced to only TRUE NORTH - VIP (removed GOLD VIP Signal, TRADE WITH HASSNIN, TWM)
- Lot allocation: 0.05 total, 0.01 per TP level (5 closes across 4 TPs)
- Profit calc example: XAUUSD signal BUY 5080-5070, SL 5065, TP1-4 hit (5082/5087/5092/5097). ~$80 profit at avg entry 5075 with 0.05 lot split 0.01/TP
- Git identity not configured on machine — commit failed, needs user.name and user.email setup
- Uncommitted fix changes still pending commit

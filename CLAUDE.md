# SignalTrader

## Service Logs

| Service | Log File |
|---------|----------|
| SignalTrader | `logs/trades.log` |

When started headless from the OpenClaw dashboard, stdout/stderr also goes to this log file.

## Profit Calculation

XAUUSD with 0.01 lot and contract size 100: **each $1 price move = $1 profit**.

For multi-position trades, each position's profit is calculated from its own entry price to its own TP, not incrementally between TPs.

Example (SELL entry ~5140):
- TP1 at 5138: profit = (5140 - 5138) × 0.01 × 100 = $2
- TP2 at 5135: profit = (5140 - 5135) × 0.01 × 100 = $5
- TP3 at 5132: profit = (5140 - 5132) × 0.01 × 100 = $8
- Total: $15 (not $2 + $3 + $3 = $8)

## SL Trailing Strategy

- TP1 hit → No SL change (let the trade breathe)
- TP2 hit → SL moves to breakeven (entry price)
- TP3 hit → SL moves to TP1 price

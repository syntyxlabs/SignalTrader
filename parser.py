"""Signal Parser — Uses Claude Code SDK to classify and parse VIP channel messages."""

import json
import logging
from typing import Optional

from claude_code_sdk import query, ClaudeCodeOptions, AssistantMessage, TextBlock

from models import Direction, OrderExecution, ParsedSignal, SignalType

log = logging.getLogger("signal_trader.parser")

SYSTEM_PROMPT = """You are a trading signal parser. Given a message from a VIP gold trading channel, classify it and extract structured data.

Return JSON only, no other text. No markdown fences, no explanation.

For NEW_SIGNAL (a new trade entry):
{"type": "NEW_SIGNAL", "execution": "MARKET" or "LIMIT", "direction": "BUY" or "SELL", "pair": "XAUUSD", "price": 5025, "price_low": 5020, "sl": null, "tp": [5027, 5032, 5037]}

Fields:
- "price": The main/upper entry price. For "PRICE 5025 - 5020", use 5025. For a single price like "PRICE 5025", use 5025.
- "price_low": The lower bound of the entry zone. For "PRICE 5025 - 5020", use 5020. For a single price, set to null.
- "sl": The stop loss if explicitly mentioned. If the signal does NOT mention an SL, set to null (the bot will auto-calculate it).
- "tp": Array of take profit levels.

The "execution" field determines order type:
- "MARKET" — execute immediately. Used when: "BUY NOW", "SELL NOW", "market buy", or just "BUY"/"SELL".
- "LIMIT" — pending limit order. Used when: "BUY LIMIT", "SELL LIMIT", or entry price is clearly away from current market.

For SL_UPDATE (stop loss modification):
{"type": "SL_UPDATE", "new_sl": 4980, "reason": "move to TP1"}

For TP_HIT (take profit level reached):
{"type": "TP_HIT", "tp_number": 3}

For CLOSE_SIGNAL (close the trade):
{"type": "CLOSE_SIGNAL", "reason": "provider says close"}

For TRAIL_STOP (activate trailing stop loss):
{"type": "TRAIL_STOP", "trail_distance": null}

For NOISE (anything that is not a trade action):
{"type": "NOISE"}

Rules:
- If the message contains a direction (BUY/SELL/BOUGHT/SOLD), a price, and at least one TP, it's NEW_SIGNAL — even if SL is missing. "BOUGHT" = BUY, "SOLD" = SELL.
- Determine MARKET vs LIMIT from the wording. "BUY NOW" / "SELL NOW" = MARKET. "BUY LIMIT" / "SELL LIMIT" = LIMIT. If ambiguous, default to MARKET.
- "Edit SL", "Move SL", "SL to breakeven", "SL to TP1" etc. are SL_UPDATE. When they say "SL to TP1", resolve TP1 to the numeric value if mentioned in the message, otherwise just set new_sl to 0 and reason to the instruction.
- "TP1 hit", "TP2 reached", "Target 3 done" etc. are TP_HIT. BUT if the message ALSO contains an SL instruction (e.g., "TP3 Hit!!! edit your stoploss to TP1"), return SL_UPDATE instead — the SL change is the actionable part.
- "Close all", "Take profit now", "Exit trade" etc. are CLOSE_SIGNAL.
- "Apply trailing stop loss", "do trailing stoploss", "lets do a trailing stop loss" etc. are TRAIL_STOP. If a distance in pips is mentioned (e.g., "every 50pips"), convert to price: trail_distance = pips / 10 (for gold, 10 pips = $1). If no distance, set trail_distance to null.
- "It hit our trailing SL", "we hit the trailing stoploss" (past tense, informational) are NOISE — the bot detects position closes automatically.
- Congratulations, commentary, news, motivation, etc. are NOISE.
- When in doubt, return NOISE. It's safer to miss a signal than to misparse one.
- The pair is always XAUUSD (gold). If a message mentions a different pair, return NOISE."""


class SignalParser:
    def __init__(self, model: str = "haiku"):
        self.model = model
        self.options = ClaudeCodeOptions(
            system_prompt=SYSTEM_PROMPT,
            max_turns=1,
            allowed_tools=[],
            model=self.model,
        )

    async def parse(self, message_text: str, timestamp: float) -> ParsedSignal:
        """Parse a Telegram message into a ParsedSignal."""
        try:
            raw_response = ""
            async for message in query(prompt=message_text, options=self.options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            raw_response += block.text

            raw_response = raw_response.strip()
            log.debug("Claude SDK response: %s", raw_response)

            if not raw_response:
                log.warning("Empty response from Claude SDK")
                return ParsedSignal(
                    type=SignalType.NOISE,
                    raw_message=message_text,
                    timestamp=timestamp,
                    reason="Empty Claude response",
                )

            return self._parse_response(raw_response, message_text, timestamp)

        except Exception as e:
            log.error("Claude SDK error: %s", e)
            return ParsedSignal(
                type=SignalType.NOISE,
                raw_message=message_text,
                timestamp=timestamp,
                reason=f"Parse error: {e}",
            )

    def _parse_response(self, raw: str, message_text: str, timestamp: float) -> ParsedSignal:
        """Parse Claude's JSON response into a ParsedSignal."""
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            raw = "\n".join(lines)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Bad JSON from Claude: %s", raw[:200])
            return ParsedSignal(
                type=SignalType.NOISE,
                raw_message=message_text,
                timestamp=timestamp,
                reason=f"Invalid JSON: {raw[:100]}",
            )

        signal_type_str = data.get("type", "NOISE")
        try:
            signal_type = SignalType(signal_type_str)
        except ValueError:
            log.warning("Unknown signal type: %s", signal_type_str)
            return ParsedSignal(
                type=SignalType.NOISE,
                raw_message=message_text,
                timestamp=timestamp,
                reason=f"Unknown type: {signal_type_str}",
            )

        base = ParsedSignal(
            type=signal_type,
            raw_message=message_text,
            timestamp=timestamp,
        )

        if signal_type == SignalType.NEW_SIGNAL:
            direction_str = data.get("direction")
            try:
                base.direction = Direction(direction_str) if direction_str else None
            except ValueError:
                base.direction = None
            exec_str = data.get("execution", "MARKET")
            try:
                base.execution = OrderExecution(exec_str)
            except ValueError:
                base.execution = OrderExecution.MARKET
            base.pair = data.get("pair")
            base.price = _to_float(data.get("price"))
            base.price_low = _to_float(data.get("price_low"))
            base.sl = _to_float(data.get("sl"))
            base.tp = [_to_float(t) for t in data.get("tp", []) if _to_float(t) is not None]

        elif signal_type == SignalType.SL_UPDATE:
            base.new_sl = _to_float(data.get("new_sl"))
            base.reason = data.get("reason")

        elif signal_type == SignalType.TP_HIT:
            base.tp_number = data.get("tp_number")

        elif signal_type == SignalType.TRAIL_STOP:
            base.trail_distance = _to_float(data.get("trail_distance"))

        elif signal_type == SignalType.CLOSE_SIGNAL:
            base.reason = data.get("reason")

        log.info("Parsed: type=%s | %s", signal_type.value, message_text[:80])
        return base


def _to_float(val) -> Optional[float]:
    """Safely convert a value to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None

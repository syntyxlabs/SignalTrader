"""Trade Manager — Position lifecycle, validation, and state persistence."""

import json
import logging
import os
import time
from dataclasses import asdict
from typing import Optional

from models import Config, Direction, OrderExecution, ParsedSignal, SignalType, TradeResult, TradeState
from mt5_client import MT5Client

log = logging.getLogger("signal_trader.trade")

KILL_SWITCH_FILE = "STOP_TRADING"


class TradeManager:
    def __init__(self, config: Config, mt5: MT5Client, base_dir: str):
        self.config = config
        self.mt5 = mt5
        self.base_dir = base_dir
        self.state_path = os.path.join(base_dir, "state.json")
        self.active_trade: Optional[TradeState] = None
        self._load_state()

    # ── Signal handling ─────────────────────────────────────────

    async def handle_signal(self, signal: ParsedSignal) -> Optional[str]:
        """
        Process a parsed signal. Returns a notification message or None.
        This is the main entry point called by channel_listener.
        """
        if self._kill_switch_active():
            log.warning("Kill switch active — ignoring signal")
            return None

        if signal.type == SignalType.NOISE:
            log.info("NOISE — ignoring: %s", signal.raw_message[:100])
            return None

        if signal.type == SignalType.NEW_SIGNAL:
            return await self._handle_new_signal(signal)

        if signal.type == SignalType.SL_UPDATE:
            return await self._handle_sl_update(signal)

        if signal.type == SignalType.TP_HIT:
            return self._handle_tp_hit(signal)

        if signal.type == SignalType.TRAIL_STOP:
            return await self._handle_trail_stop(signal)

        if signal.type == SignalType.CLOSE_SIGNAL:
            return await self._handle_close(signal)

        return None

    # ── NEW_SIGNAL ──────────────────────────────────────────────

    async def _handle_new_signal(self, signal: ParsedSignal) -> Optional[str]:
        """Validate and execute a new trade signal (market or limit)."""
        # Rule 1: One trade at a time — but allow SL/TP updates from edits
        if self.active_trade is not None:
            return await self._maybe_update_from_edit(signal)

        # Also check MT5 for positions + pending orders we don't know about
        positions = await self.mt5.get_open_positions_async()
        pending = await self.mt5.get_pending_orders_async()
        if len(positions) + len(pending) >= self.config.max_open_trades:
            log.info("MT5 already has %d position(s) + %d pending — ignoring",
                     len(positions), len(pending))
            return None

        # Auto-calculate SL if missing
        if signal.sl is None or signal.sl <= 0:
            signal.sl = self._auto_calculate_sl(signal)
            if signal.sl is None:
                log.warning("Could not auto-calculate SL")
                return "Signal rejected: No SL and could not auto-calculate"
            log.info("Auto-calculated SL: %.2f", signal.sl)

        # Clamp SL to max distance (don't reject, just tighten)
        sl_distance = abs(signal.price - signal.sl)
        if sl_distance > self.config.max_sl_distance:
            if signal.direction == Direction.BUY:
                signal.sl = signal.price - self.config.max_sl_distance
            else:
                signal.sl = signal.price + self.config.max_sl_distance
            log.info("SL clamped: $%.2f too far, using $%.2f distance → SL %.2f",
                     sl_distance, self.config.max_sl_distance, signal.sl)

        # Validate parsed fields
        error = self._validate_new_signal(signal)
        if error:
            log.warning("Signal validation failed: %s", error)
            return f"Signal rejected: {error}"

        is_limit = signal.execution == OrderExecution.LIMIT
        tp_distance = self.config.fixed_tp_distance  # 0 = disabled

        # Execute: market or limit
        if is_limit:
            result = await self.mt5.open_limit_order_async(
                signal.direction, self.config.lot_size, signal.price, signal.sl, tp_distance)
        else:
            result = await self.mt5.open_position_async(
                signal.direction, self.config.lot_size, signal.sl, tp_distance)

        if not result.success:
            msg = f"Order failed: {result.error_message}"
            log.error(msg)
            return msg

        # Save state
        self.active_trade = TradeState(
            ticket=result.ticket,
            direction=signal.direction,
            pair=signal.pair,
            entry_price=result.price or signal.price,
            signal_price=signal.price,
            current_sl=signal.sl,
            tp_levels=signal.tp,
            lot_size=self.config.lot_size,
            opened_at=time.time(),
            last_updated=time.time(),
            is_pending=is_limit,
            order_ticket=result.ticket if is_limit else None,
        )
        self._save_state()

        order_type = "LIMIT" if is_limit else "MARKET"
        actual_tp = (result.price or signal.price) + tp_distance if signal.direction == Direction.BUY else (result.price or signal.price) - tp_distance
        tp_str = f"Fixed TP: {actual_tp:.2f}" if tp_distance > 0 else f"TPs: {', '.join(f'{t:.0f}' for t in signal.tp)}"
        msg = (
            f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
            f"{signal.direction.value} {order_type} {signal.pair} @ {result.price or signal.price:.2f}\n"
            f"   SL: {signal.sl:.2f} | {tp_str}\n"
            f"   Lot: {self.config.lot_size} | Risk: ${abs(signal.price - signal.sl) * self.config.lot_size * 100:.2f}"
        )
        log.info(msg)
        return msg

    async def _maybe_update_from_edit(self, signal: ParsedSignal) -> Optional[str]:
        """When we already have a trade and get a NEW_SIGNAL edit, update SL/TPs if better."""
        trade = self.active_trade

        # Must be same direction and pair
        if signal.direction != trade.direction or signal.pair != trade.pair:
            log.info("Trade already open (ticket=%d) — ignoring different signal", trade.ticket)
            return None

        updates = []

        # Update SL if signal has an explicit one that differs from ours
        if signal.sl is not None and signal.sl > 0 and signal.sl != trade.current_sl:
            result = await self.mt5.modify_sl_async(trade.ticket, signal.sl)
            if result.success:
                old_sl = trade.current_sl
                trade.current_sl = signal.sl
                updates.append(f"SL: {old_sl:.2f} -> {signal.sl:.2f}")
            else:
                log.error("SL update from edit failed: %s", result.error_message)

        # Update TP levels if signal has more
        if signal.tp and len(signal.tp) > len(trade.tp_levels):
            trade.tp_levels = signal.tp
            updates.append(f"TPs: {len(signal.tp)} levels")

        if updates:
            trade.last_updated = time.time()
            self._save_state()
            msg = (
                f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
                f"Trade updated from edit — {', '.join(updates)}"
            )
            log.info(msg)
            return msg

        return None

    def _auto_calculate_sl(self, signal: ParsedSignal) -> Optional[float]:
        """Auto-calculate SL when signal doesn't provide one.

        Logic:
        - BUY: SL = (price_low or price) - default_sl_distance
        - SELL: SL = (price or price_high) + default_sl_distance

        For "PRICE 5025 - 5020" BUY → SL = 5020 - 10 = 5010
        """
        if signal.direction is None or signal.price is None:
            return None

        dist = self.config.default_sl_distance

        if signal.direction == Direction.BUY:
            base = signal.price_low if signal.price_low else signal.price
            return base - dist

        if signal.direction == Direction.SELL:
            base = signal.price  # price is the upper bound
            return base + dist

        return None

    def _validate_new_signal(self, signal: ParsedSignal) -> Optional[str]:
        """Validate a NEW_SIGNAL. Returns error string or None if valid."""
        # Direction
        if signal.direction is None:
            return "Missing direction"

        # Pair must match config
        if signal.pair != self.config.pair:
            return f"Wrong pair: {signal.pair} (expected {self.config.pair})"

        # Price must be positive
        if signal.price is None or signal.price <= 0:
            return "Invalid price"

        # SL must be set
        if signal.sl is None or signal.sl <= 0:
            return "Invalid SL"

        # At least 1 TP
        if not signal.tp:
            return "No TP levels"

        # Rule 5: SL direction
        if signal.direction == Direction.BUY and signal.sl >= signal.price:
            return f"BUY SL ({signal.sl}) must be below price ({signal.price})"
        if signal.direction == Direction.SELL and signal.sl <= signal.price:
            return f"SELL SL ({signal.sl}) must be above price ({signal.price})"

        # Rule 6: Stale signal
        age = time.time() - signal.timestamp
        if age > self.config.stale_signal_seconds:
            return f"Signal too old ({age:.0f}s > {self.config.stale_signal_seconds}s)"

        # Rule 8: Price deviation from current market
        # Note: We do async price check before calling this, but as a sync fallback
        # the mt5 price check happens in open_position itself

        # Rule 9: Max SL distance — now handled by clamping in _handle_new_signal

        return None

    # ── SL_UPDATE ───────────────────────────────────────────────

    async def _handle_sl_update(self, signal: ParsedSignal) -> Optional[str]:
        """Modify the stop loss of the active trade."""
        if self.active_trade is None:
            log.info("No active trade — ignoring SL update")
            return None

        if self.active_trade.is_pending:
            log.info("Trade is still pending — ignoring SL update")
            return None

        new_sl = signal.new_sl

        # If new_sl is 0 or None, the parser couldn't resolve the TP reference
        if new_sl is None or new_sl <= 0:
            # Try to resolve from reason (e.g., "move to TP1")
            new_sl = self._resolve_tp_reference(signal.reason)
            if new_sl is None:
                log.warning("Could not resolve SL value from: %s", signal.reason)
                return f"Could not resolve SL from: {signal.reason}"

        # Validate SL direction
        if self.active_trade.direction == Direction.BUY and new_sl <= self.active_trade.current_sl:
            log.info("New SL (%.2f) is not above current SL (%.2f) for BUY — ignoring",
                     new_sl, self.active_trade.current_sl)
            return None

        if self.active_trade.direction == Direction.SELL and new_sl >= self.active_trade.current_sl:
            log.info("New SL (%.2f) is not below current SL (%.2f) for SELL — ignoring",
                     new_sl, self.active_trade.current_sl)
            return None

        result = await self.mt5.modify_sl_async(self.active_trade.ticket, new_sl)

        if not result.success:
            msg = f"SL modify failed: {result.error_message}"
            log.error(msg)
            return msg

        old_sl = self.active_trade.current_sl
        self.active_trade.current_sl = new_sl
        self.active_trade.last_updated = time.time()
        self._save_state()

        msg = (
            f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
            f"SL updated: {old_sl:.2f} -> {new_sl:.2f}"
            f"{f' ({signal.reason})' if signal.reason else ''}"
        )
        log.info(msg)
        return msg

    def _resolve_tp_reference(self, reason: Optional[str]) -> Optional[float]:
        """Try to resolve 'TP1', 'TP2' etc. to actual values from active trade."""
        if not reason or not self.active_trade:
            return None

        reason_upper = reason.upper()
        for i, tp_val in enumerate(self.active_trade.tp_levels, 1):
            if f"TP{i}" in reason_upper or f"TP {i}" in reason_upper:
                return tp_val

        # Try "breakeven" or "entry"
        if "BREAKEVEN" in reason_upper or "ENTRY" in reason_upper:
            return self.active_trade.entry_price

        return None

    # ── TRAIL_STOP ────────────────────────────────────────────────

    async def _handle_trail_stop(self, signal: ParsedSignal) -> Optional[str]:
        """Activate trailing stop on the active trade."""
        if self.active_trade is None:
            log.info("No active trade — ignoring trail stop")
            return None

        if self.active_trade.is_pending:
            log.info("Trade is still pending — ignoring trail stop")
            return None

        if self.active_trade.trail_active:
            log.info("Trailing stop already active (distance=%.2f)", self.active_trade.trail_distance)
            return None

        # Use signal distance or config default
        distance = signal.trail_distance or self.config.default_trail_distance

        # Get current price to initialize trail_price
        current_price = await self.mt5.get_current_price_async()
        if current_price is None:
            return "Trail stop failed: cannot get current price"

        self.active_trade.trail_active = True
        self.active_trade.trail_distance = distance
        self.active_trade.trail_price = current_price
        self.active_trade.last_updated = time.time()
        self._save_state()

        msg = (
            f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
            f"Trailing stop ACTIVATED — distance: {distance:.2f}, "
            f"starting price: {current_price:.2f}"
        )
        log.info(msg)
        return msg

    # ── Trailing SL update (called from poll loop) ─────────────

    async def _update_trailing_sl(self) -> Optional[str]:
        """Check current price and ratchet SL if price moved favorably."""
        current_price = await self.mt5.get_current_price_async()
        if current_price is None:
            log.warning("Trailing: cannot get current price")
            return None

        trade = self.active_trade
        distance = trade.trail_distance

        if trade.direction == Direction.BUY:
            # Track highest price, SL follows below it
            if current_price > trade.trail_price:
                trade.trail_price = current_price

            new_sl = trade.trail_price - distance

            # Only move SL up, never down
            if new_sl <= trade.current_sl:
                return None

        elif trade.direction == Direction.SELL:
            # Track lowest price, SL follows above it
            if current_price < trade.trail_price:
                trade.trail_price = current_price

            new_sl = trade.trail_price + distance

            # Only move SL down, never up
            if new_sl >= trade.current_sl:
                return None
        else:
            return None

        # Round to 2 decimals for gold
        new_sl = round(new_sl, 2)

        result = await self.mt5.modify_sl_async(trade.ticket, new_sl)

        if not result.success:
            log.error("Trailing SL modify failed: %s", result.error_message)
            return None

        old_sl = trade.current_sl
        trade.current_sl = new_sl
        trade.last_updated = time.time()
        self._save_state()

        msg = (
            f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
            f"Trailing SL: {old_sl:.2f} -> {new_sl:.2f} "
            f"(price: {current_price:.2f}, best: {trade.trail_price:.2f})"
        )
        log.info(msg)
        return msg

    # ── TP_HIT ──────────────────────────────────────────────────

    def _handle_tp_hit(self, signal: ParsedSignal) -> Optional[str]:
        """Log a TP hit (informational only at 0.01 lot)."""
        if self.active_trade is None:
            return None

        tp_num = signal.tp_number or 0
        tp_price = None
        if 1 <= tp_num <= len(self.active_trade.tp_levels):
            tp_price = self.active_trade.tp_levels[tp_num - 1]

        msg = f"TP{tp_num} hit!{f' ({tp_price:.2f})' if tp_price else ''}"
        log.info(msg)
        return msg

    # ── CLOSE_SIGNAL ────────────────────────────────────────────

    async def _handle_close(self, signal: ParsedSignal) -> Optional[str]:
        """Close the active trade or cancel pending order."""
        if self.active_trade is None:
            log.info("No active trade — ignoring close signal")
            return None

        # If it's a pending order, cancel it instead of closing
        if self.active_trade.is_pending and self.active_trade.order_ticket:
            result = await self.mt5.cancel_order_async(self.active_trade.order_ticket)
            action = "Pending order cancelled"
        else:
            result = await self.mt5.close_position_async(self.active_trade.ticket)
            action = f"Position closed @ {result.price or 0:.2f}"

        if not result.success:
            msg = f"Close/cancel failed: {result.error_message}"
            log.error(msg)
            return msg

        msg = (
            f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
            f"{action}"
            f"{f' — {signal.reason}' if signal.reason else ''}"
        )
        log.info(msg)

        self.active_trade = None
        self._save_state()
        return msg

    # ── Position polling ────────────────────────────────────────

    async def check_position_status(self) -> Optional[str]:
        """
        Poll MT5 to check if position/order is still alive.
        Handles: pending→filled transition, position closed externally, pending cancelled.
        """
        if self.active_trade is None:
            return None

        # If pending limit order, check if it got filled or cancelled
        if self.active_trade.is_pending:
            return await self._check_pending_status()

        # For open positions, check if still alive
        positions = await self.mt5.get_open_positions_async()
        our_tickets = {p.ticket for p in positions}

        if self.active_trade.ticket not in our_tickets:
            msg = (
                f"Position {self.active_trade.ticket} closed externally "
                f"(SL/TP hit or manual close)"
            )
            log.info(msg)
            self.active_trade = None
            self._save_state()
            return msg

        # Trailing stop logic — ratchet SL as price moves in our favor
        if self.active_trade.trail_active:
            return await self._update_trailing_sl()

        return None

    async def _check_pending_status(self) -> Optional[str]:
        """Check if a pending order was filled or cancelled."""
        # Check if the pending order still exists
        pending_orders = await self.mt5.get_pending_orders_async()
        pending_tickets = {o.ticket for o in pending_orders}

        if self.active_trade.order_ticket in pending_tickets:
            # Still pending, nothing to do
            return None

        # Order is gone — check if it became a position (filled)
        positions = await self.mt5.get_open_positions_async()

        # Look for a position opened by our magic number with matching symbol
        our_position = None
        for p in positions:
            if p.magic == 123456 and p.symbol == self.config.mt5_symbol:
                our_position = p
                break

        if our_position:
            # Pending order was filled — transition to active position
            self.active_trade.is_pending = False
            self.active_trade.ticket = our_position.ticket
            self.active_trade.entry_price = our_position.price_open
            self.active_trade.order_ticket = None
            self.active_trade.last_updated = time.time()
            self._save_state()

            msg = (
                f"Limit order FILLED — {self.active_trade.direction.value} "
                f"{self.active_trade.pair} @ {our_position.price_open:.2f}"
            )
            log.info(msg)
            return msg
        else:
            # Order was cancelled/expired without filling
            msg = f"Pending order {self.active_trade.order_ticket} cancelled/expired"
            log.info(msg)
            self.active_trade = None
            self._save_state()
            return msg

    # ── Startup reconciliation ──────────────────────────────────

    async def reconcile(self) -> None:
        """Reconcile state.json with actual MT5 positions on startup."""
        positions = await self.mt5.get_open_positions_async()
        has_state = self.active_trade is not None
        has_position = len(positions) > 0

        if has_state and has_position:
            # Check if our tracked position is still open
            our_tickets = {p.ticket for p in positions}
            if self.active_trade.ticket in our_tickets:
                log.info("Reconcile: active trade ticket=%d still open — resuming",
                         self.active_trade.ticket)
            else:
                log.warning("Reconcile: tracked ticket=%d not found in MT5 — clearing state",
                            self.active_trade.ticket)
                self.active_trade = None
                self._save_state()

        elif has_state and not has_position:
            log.info("Reconcile: state has trade but MT5 has no positions — cleared (closed during downtime)")
            self.active_trade = None
            self._save_state()

        elif not has_state and has_position:
            log.warning("Reconcile: MT5 has %d position(s) but no state — NOT managing (may be manual)",
                        len(positions))

        else:
            log.info("Reconcile: clean slate — no state, no positions")

    # ── State persistence ───────────────────────────────────────

    def _load_state(self) -> None:
        """Load active trade from state.json."""
        if not os.path.exists(self.state_path):
            self.active_trade = None
            return

        try:
            with open(self.state_path) as f:
                data = json.load(f)

            trade_data = data.get("trade")
            if trade_data is None:
                self.active_trade = None
                return

            trade_data["direction"] = Direction(trade_data["direction"])
            self.active_trade = TradeState(**trade_data)
            log.info("Loaded active trade: ticket=%d", self.active_trade.ticket)

        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.error("Corrupted state.json: %s — treating as no active trade", e)
            self.active_trade = None

    def _save_state(self) -> None:
        """Atomically save active trade to state.json."""
        if self.active_trade is None:
            data = {"version": 1, "trade": None}
        else:
            trade_dict = asdict(self.active_trade)
            trade_dict["direction"] = self.active_trade.direction.value
            data = {"version": 1, "trade": trade_dict}

        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.state_path)

    # ── Helpers ─────────────────────────────────────────────────

    def _kill_switch_active(self) -> bool:
        return os.path.exists(os.path.join(self.base_dir, KILL_SWITCH_FILE))

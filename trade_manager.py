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


class PositionCounter:
    """Enforces a global max positions limit across all channels."""

    def __init__(self, max_positions: int):
        self._max = max_positions
        self._managers: list["TradeManager"] = []

    def register(self, manager: "TradeManager") -> None:
        self._managers.append(manager)

    def active_count(self) -> int:
        return sum(1 for m in self._managers if m.active_trade is not None)

    def can_open(self) -> bool:
        return self.active_count() < self._max


class TradeManager:
    def __init__(self, config: Config, mt5: MT5Client, base_dir: str,
                 channel_name: str = "", position_counter: "PositionCounter" = None,
                 state_file: str = "state.json"):
        self.config = config
        self.mt5 = mt5
        self.base_dir = base_dir
        self.channel_name = channel_name
        self.position_counter = position_counter
        self.state_path = os.path.join(base_dir, state_file)
        self.active_trade: Optional[TradeState] = None
        self._load_state()

    def _notify(self, msg: str) -> str:
        """Prefix notification with channel name."""
        if self.channel_name:
            return f"[{self.channel_name}] {msg}"
        return msg

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

        result = None
        if signal.type == SignalType.NEW_SIGNAL:
            result = await self._handle_new_signal(signal)
        elif signal.type == SignalType.SL_UPDATE:
            result = await self._handle_sl_update(signal)
        elif signal.type == SignalType.TP_HIT:
            result = await self._handle_tp_hit(signal)
        elif signal.type == SignalType.TRAIL_STOP:
            result = await self._handle_trail_stop(signal)
        elif signal.type == SignalType.CLOSE_SIGNAL:
            result = await self._handle_close(signal)

        return self._notify(result) if result else None

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

        # Global position cap across all channels
        if self.position_counter and not self.position_counter.can_open():
            log.info("Global position cap reached (%d/%d) — ignoring signal from [%s]",
                     self.position_counter.active_count(), self.config.max_positions,
                     self.channel_name)
            return None

        # Auto-calculate SL if missing
        if signal.sl is None or signal.sl <= 0:
            signal.sl = self._auto_calculate_sl(signal)
            if signal.sl is None:
                log.warning("Could not auto-calculate SL")
                return "Signal rejected: No SL and could not auto-calculate"
            log.info("Auto-calculated SL: %.2f", signal.sl)

        # Use the signal provider's SL as-is (no clamping)
        sl_distance = abs(signal.price - signal.sl)
        log.info("Signal SL: %.2f ($%.2f from entry)", signal.sl, sl_distance)

        # Validate parsed fields
        error = self._validate_new_signal(signal)
        if error:
            log.warning("Signal validation failed: %s", error)
            return f"Signal rejected: {error}"

        # Price deviation check — reject if market has moved too far from signal price
        current_price = await self.mt5.get_current_price_async()
        if current_price is not None:
            deviation = abs(current_price - signal.price)
            if deviation > self.config.max_price_deviation:
                msg = f"Signal rejected: price deviation ${deviation:.2f} > ${self.config.max_price_deviation:.2f} (signal={signal.price:.2f}, market={current_price:.2f})"
                log.warning(msg)
                return msg

        is_limit = signal.execution == OrderExecution.LIMIT
        tp_distance = self.config.fixed_tp_distance  # 0 = use signal TPs

        # Decide: multi-position (separate TP per position) or single position
        use_multi = (
            tp_distance <= 0
            and signal.tp
            and self.config.close_lot_per_tp > 0
        )

        pending_order_tickets = []

        if use_multi:
            tickets, entry_price, total_lot = await self._open_multi_positions(signal, is_limit=is_limit)
            if is_limit:
                sub_tickets = []
                pending_order_tickets = tickets
            else:
                sub_tickets = tickets
        elif is_limit:
            result = await self.mt5.open_limit_order_async(
                signal.direction, self.config.lot_size, signal.price, signal.sl, tp_distance)
            if not result.success:
                return f"Order failed: {result.error_message}"
            sub_tickets = []
            pending_order_tickets = [result.ticket]
            entry_price = result.price or signal.price
            total_lot = self.config.lot_size
        else:
            # Single market position with fixed TP
            result = await self.mt5.open_position_async(
                signal.direction, self.config.lot_size, signal.sl, tp_distance)
            if not result.success:
                return f"Order failed: {result.error_message}"
            sub_tickets = [result.ticket]
            entry_price = result.price or signal.price
            total_lot = self.config.lot_size

        if not sub_tickets and not pending_order_tickets:
            return "Order failed: all positions failed to open"

        # Save state
        self.active_trade = TradeState(
            ticket=sub_tickets[0] if sub_tickets else 0,
            direction=signal.direction,
            pair=signal.pair,
            entry_price=entry_price,
            signal_price=signal.price,
            current_sl=signal.sl,
            tp_levels=signal.tp or [],
            lot_size=round(total_lot, 2),
            opened_at=time.time(),
            last_updated=time.time(),
            is_pending=bool(pending_order_tickets),
            order_ticket=pending_order_tickets[0] if pending_order_tickets else None,
            remaining_lot=round(total_lot, 2),
            sub_tickets=sub_tickets,
            pending_order_tickets=pending_order_tickets,
        )
        self._save_state()

        # Build notification
        order_type = "LIMIT" if is_limit else "MARKET"
        if use_multi:
            num_orders = len(sub_tickets) + len(pending_order_tickets)
            tp_str = " | ".join(f"TP{i+1}: {signal.tp[i]:.2f}" for i in range(min(num_orders, len(signal.tp))))
            kind = "orders" if is_limit else "positions"
            lot_str = f"{num_orders} {kind} x {self.config.close_lot_per_tp} lot = {total_lot:.2f}"
        elif tp_distance > 0:
            actual_tp = entry_price + tp_distance if signal.direction == Direction.BUY else entry_price - tp_distance
            tp_str = f"Fixed TP: {actual_tp:.2f}"
            lot_str = f"Lot: {total_lot}"
        else:
            tp_str = f"TPs: {', '.join(f'{t:.0f}' for t in signal.tp)}"
            lot_str = f"Lot: {total_lot}"

        msg = (
            f"{'[DRY-RUN] ' if self.config.dry_run else ''}"
            f"{signal.direction.value} {order_type} {signal.pair} @ {entry_price:.2f}\n"
            f"   SL: {signal.sl:.2f} | {tp_str}\n"
            f"   {lot_str}"
        )
        log.info(msg)
        return msg

    async def _open_multi_positions(self, signal: ParsedSignal, is_limit: bool = False) -> tuple[list[int], float, float]:
        """Open N separate positions/limit orders, each with its own TP. Returns (tickets, entry_price, total_lot)."""
        max_splits = max(1, int(round(self.config.lot_size / self.config.close_lot_per_tp)))

        # Get current market price for TP validation (ask for BUY, bid for SELL)
        current_price = await self.mt5.get_current_price_async()
        ref_price = current_price if current_price else signal.price

        # Filter out TPs on the wrong side of current market price
        valid_tps = []
        for tp in signal.tp:
            if signal.direction == Direction.BUY and tp > ref_price:
                valid_tps.append(tp)
            elif signal.direction == Direction.SELL and tp < ref_price:
                valid_tps.append(tp)
            else:
                log.warning("Invalid TP %.2f (wrong side of market price %.2f for %s) — will use default",
                            tp, ref_price, signal.direction.value)

        # Pad with default TPs ($2, $5, $8 from current price) if not enough valid ones
        default_distances = [2.0, 5.0, 8.0]
        if len(valid_tps) < max_splits:
            for d in default_distances:
                if len(valid_tps) >= max_splits:
                    break
                if signal.direction == Direction.BUY:
                    default_tp = ref_price + d
                else:
                    default_tp = ref_price - d
                # Don't duplicate existing TPs
                if default_tp not in valid_tps:
                    valid_tps.append(default_tp)
                    log.info("Using default TP: %.2f ($%.0f from entry)", default_tp, d)

            # Sort: ascending for BUY (closest first), descending for SELL
            valid_tps.sort(reverse=(signal.direction == Direction.SELL))
        num_positions = min(len(valid_tps), max_splits)

        sub_tickets = []
        entry_price = None
        total_lot = 0.0
        remaining = self.config.lot_size

        for i in range(num_positions):
            # Last position gets the remainder (handles rounding)
            if i < num_positions - 1:
                lot = self.config.close_lot_per_tp
            else:
                lot = round(remaining, 2)

            tp = valid_tps[i]

            if is_limit:
                result = await self.mt5.open_limit_order_async(
                    signal.direction, lot, signal.price, signal.sl, tp_price=tp)
            else:
                result = await self.mt5.open_position_async(
                    signal.direction, lot, signal.sl, tp_price=tp)

            kind = "limit order" if is_limit else "position"
            if result.success:
                sub_tickets.append(result.ticket)
                total_lot += lot
                remaining -= lot
                if entry_price is None:
                    entry_price = result.price or signal.price
                log.info("Sub-%s %d/%d opened: ticket=%s, lot=%.2f, TP=%.2f",
                         kind, i + 1, num_positions, result.ticket, lot, tp)
            else:
                # Failed — lot carries forward to next position
                log.warning("Sub-%s %d/%d failed: %s — lot carries forward",
                            kind, i + 1, num_positions, result.error_message)

        return sub_tickets, entry_price or signal.price, total_lot

    async def _maybe_update_from_edit(self, signal: ParsedSignal) -> Optional[str]:
        """When we already have a trade and get a NEW_SIGNAL edit, update SL/TPs if better."""
        trade = self.active_trade

        # Must be same direction and pair
        if signal.direction != trade.direction or signal.pair != trade.pair:
            log.info("Trade already open (ticket=%d) — ignoring different signal", trade.ticket)
            return None

        updates = []

        # Update SL if signal has an explicit one that differs from ours — always follow provider
        if signal.sl is not None and signal.sl > 0 and signal.sl != trade.current_sl:
            # After TP hits, don't accept SL that's worse than current (protect breakeven/trailing)
            if trade.tp_hits_count > 0:
                if trade.direction == Direction.BUY and signal.sl < trade.current_sl:
                    log.info("Rejecting SL edit %.2f — worse than post-TP SL %.2f (BUY)",
                             signal.sl, trade.current_sl)
                    signal.sl = None
                elif trade.direction == Direction.SELL and signal.sl > trade.current_sl:
                    log.info("Rejecting SL edit %.2f — worse than post-TP SL %.2f (SELL)",
                             signal.sl, trade.current_sl)
                    signal.sl = None

            if signal.sl is not None and signal.sl > 0 and signal.sl != trade.current_sl:
                # Cap SL distance to max_sl_distance
                sl_distance = abs(trade.entry_price - signal.sl)
                if sl_distance > self.config.max_sl_distance:
                    if trade.direction == Direction.BUY:
                        signal.sl = trade.entry_price - self.config.max_sl_distance
                    else:
                        signal.sl = trade.entry_price + self.config.max_sl_distance
                    log.warning("Provider SL too wide ($%.2f) — clamped to $%.2f (SL=%.2f)",
                                sl_distance, self.config.max_sl_distance, signal.sl)

                ok = await self._modify_sl_all(signal.sl)
                if ok:
                    old_sl = trade.current_sl
                    trade.current_sl = signal.sl
                    updates.append(f"SL: {old_sl:.2f} -> {signal.sl:.2f}")

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

        For "PRICE 5025 - 5020" BUY -> SL = 5020 - 10 = 5010
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

        # At least 1 TP required (fixed_tp_distance can override if set)
        if not signal.tp and self.config.fixed_tp_distance <= 0:
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

        return None

    # ── SL_UPDATE ───────────────────────────────────────────────

    async def _handle_sl_update(self, signal: ParsedSignal) -> Optional[str]:
        """Modify the stop loss of the active trade (all sub-positions)."""
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

        ok = await self._modify_sl_all(new_sl)

        if not ok:
            msg = "SL modify failed on one or more positions"
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

        ok = await self._modify_sl_all(new_sl)

        if not ok:
            log.error("Trailing SL modify failed on one or more positions")
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

    async def _handle_tp_hit(self, signal: ParsedSignal) -> Optional[str]:
        """Handle a TP hit signal — sync with MT5 and trail SL on remaining positions."""
        if self.active_trade is None:
            return None

        # Sync sub-positions with MT5 (the TP hit may have already been detected by poll)
        result = await self._sync_sub_positions()

        tp_num = signal.tp_number or self.active_trade.tp_hits_count if self.active_trade else 0
        tp_price = None
        if self.active_trade and 1 <= tp_num <= len(self.active_trade.tp_levels):
            tp_price = self.active_trade.tp_levels[tp_num - 1]

        if result:
            return result

        # If sync found nothing new, just log the signal
        msg = f"TP{tp_num} signal received{f' ({tp_price:.2f})' if tp_price else ''} — already handled"
        log.info(msg)
        return msg

    # ── CLOSE_SIGNAL ────────────────────────────────────────────

    async def _handle_close(self, signal: ParsedSignal) -> Optional[str]:
        """Close all sub-positions or cancel pending order."""
        if self.active_trade is None:
            log.info("No active trade — ignoring close signal")
            return None

        # If it's a pending order, cancel it instead of closing
        if self.active_trade.is_pending and self.active_trade.order_ticket:
            result = await self.mt5.cancel_order_async(self.active_trade.order_ticket)
            if not result.success:
                return f"Cancel failed: {result.error_message}"
            action = "Pending order cancelled"
        else:
            # Close all open sub-positions
            closed, failed = await self._close_all_positions()
            action = f"{closed} position(s) closed"
            if failed > 0:
                action += f" ({failed} failed)"

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
        Detects TP hits (positions auto-closed by MT5), trails SL on remaining.
        """
        if self.active_trade is None:
            return None

        # If pending limit order, check if it got filled or cancelled
        if self.active_trade.is_pending:
            result = await self._check_pending_status()
            return self._notify(result) if result else None

        # Sync sub-positions: detect TP hits, trail SL, clear if all closed
        result = await self._sync_sub_positions()
        if result:
            return self._notify(result)

        # Trailing stop logic — ratchet SL as price moves in our favor
        if self.active_trade and self.active_trade.trail_active:
            result = await self._update_trailing_sl()
            return self._notify(result) if result else None

        return None

    async def _sync_sub_positions(self) -> Optional[str]:
        """Check all sub-positions against MT5, detect closures, trail SL.

        Returns a notification message if something changed, None otherwise.
        """
        trade = self.active_trade
        if trade is None:
            return None

        sub_tickets = trade.sub_tickets or [trade.ticket]

        positions = await self.mt5.get_open_positions_async()
        open_mt5 = {p.ticket for p in positions}

        still_open = [t for t in sub_tickets if t in open_mt5]
        expected_open = len(sub_tickets) - trade.tp_hits_count
        newly_closed = expected_open - len(still_open)

        if newly_closed <= 0 and len(still_open) > 0:
            return None  # No changes

        msgs = []

        if newly_closed > 0:
            old_hits = trade.tp_hits_count
            trade.tp_hits_count += newly_closed

            # Calculate remaining lot from actual MT5 positions
            trade.remaining_lot = round(
                sum(p.volume for p in positions if p.ticket in set(still_open)), 2)

            for i in range(old_hits + 1, trade.tp_hits_count + 1):
                tp_price = trade.tp_levels[i - 1] if i <= len(trade.tp_levels) else None
                msgs.append(f"TP{i} hit{f' ({tp_price:.2f})' if tp_price else ''}!")

            msgs.append(f"{len(still_open)} pos remaining ({trade.remaining_lot:.2f} lot)")

            # Trail SL on remaining positions
            new_sl = self._get_sl_after_tp(trade.tp_hits_count)
            if new_sl is not None and len(still_open) > 0:
                ok = await self._modify_sl_all(new_sl)
                if ok:
                    trade.current_sl = new_sl
                    msgs.append(f"SL ->{new_sl:.2f}")

        # All positions closed
        if len(still_open) == 0:
            if not msgs:
                remaining = trade.remaining_lot
                msgs.append(
                    f"All positions closed externally"
                    f"{f' — {remaining:.2f} lot was still open' if remaining > 0 else ''}"
                )
            else:
                msgs.append("Trade fully closed")

            self.active_trade = None
            self._save_state()
            msg = " | ".join(msgs)
            log.info(msg)
            return f"{'[DRY-RUN] ' if self.config.dry_run else ''}{msg}"

        trade.last_updated = time.time()
        self._save_state()
        msg = " | ".join(msgs)
        log.info(msg)
        return f"{'[DRY-RUN] ' if self.config.dry_run else ''}{msg}" if msgs else None

    def _get_sl_after_tp(self, tp_hits: int) -> Optional[float]:
        """Determine new SL after TP hits: TP1→breakeven, TP2→TP1, TP3→TP2, etc."""
        trade = self.active_trade
        if trade is None or tp_hits <= 0:
            return None

        if tp_hits == 1:
            return trade.entry_price  # Breakeven

        # TP2+ → SL to previous TP level
        idx = tp_hits - 2  # 0-based index into tp_levels
        if idx < len(trade.tp_levels):
            return trade.tp_levels[idx]

        return None

    async def _check_pending_status(self) -> Optional[str]:
        """Check if pending orders were filled or cancelled (supports multi-order)."""
        trade = self.active_trade

        # Collect all pending order tickets we're tracking
        tracked_orders = trade.pending_order_tickets or (
            [trade.order_ticket] if trade.order_ticket else [])

        if not tracked_orders:
            return None

        # Check which orders are still pending in MT5
        pending_orders = await self.mt5.get_pending_orders_async()
        pending_set = {o.ticket for o in pending_orders}
        still_pending = [t for t in tracked_orders if t in pending_set]
        newly_gone = [t for t in tracked_orders if t not in pending_set]

        if not newly_gone:
            return None  # All still pending

        # Find new positions (filled orders become positions with new ticket numbers)
        positions = await self.mt5.get_open_positions_async()
        known_tickets = set(trade.sub_tickets)
        new_positions = [p for p in positions
                         if p.magic == 123456
                         and p.symbol == self.config.mt5_symbol
                         and p.ticket not in known_tickets]

        msgs = []

        if new_positions:
            for p in new_positions:
                trade.sub_tickets.append(p.ticket)
            if trade.ticket == 0:
                trade.ticket = new_positions[0].ticket
            trade.entry_price = new_positions[0].price_open
            msgs.append(f"{len(new_positions)} limit order(s) filled @ {trade.entry_price:.2f}")

        # Update pending tracking
        trade.pending_order_tickets = still_pending

        # Check if all orders are resolved
        if not still_pending:
            if trade.sub_tickets:
                # All resolved — transition to active position tracking
                trade.is_pending = False
                trade.order_ticket = None
                trade.remaining_lot = round(
                    sum(p.volume for p in positions if p.ticket in set(trade.sub_tickets)), 2)
                trade.lot_size = trade.remaining_lot
                trade.last_updated = time.time()
                self._save_state()

                msg = (
                    f"Limit order FILLED — {trade.direction.value} "
                    f"{trade.pair} @ {trade.entry_price:.2f}\n"
                    f"   {len(trade.sub_tickets)} positions x {self.config.close_lot_per_tp} lot"
                )
                log.info(msg)
                return msg
            else:
                # All cancelled/expired, no fills
                msg = f"All pending orders cancelled/expired ({len(newly_gone)} orders)"
                log.info(msg)
                self.active_trade = None
                self._save_state()
                return msg

        # Some filled, some still pending
        if msgs:
            trade.last_updated = time.time()
            self._save_state()
            msg = " | ".join(msgs) + f" | {len(still_pending)} order(s) still pending"
            log.info(msg)
            return msg

        return None

    # ── Startup reconciliation ──────────────────────────────────

    async def reconcile(self) -> None:
        """Reconcile state.json with actual MT5 positions on startup."""
        positions = await self.mt5.get_open_positions_async()
        has_state = self.active_trade is not None
        has_position = len(positions) > 0

        # Handle pending limit orders on restart
        if has_state and self.active_trade.is_pending:
            pending_tickets = self.active_trade.pending_order_tickets or (
                [self.active_trade.order_ticket] if self.active_trade.order_ticket else [])
            pending_orders = await self.mt5.get_pending_orders_async()
            pending_set = {o.ticket for o in pending_orders}
            still_pending = [t for t in pending_tickets if t in pending_set]

            # Check for filled orders that became positions
            known = set(self.active_trade.sub_tickets)
            new_pos = [p for p in positions
                       if p.magic == 123456 and p.symbol == self.config.mt5_symbol
                       and p.ticket not in known]
            for p in new_pos:
                self.active_trade.sub_tickets.append(p.ticket)
                if self.active_trade.ticket == 0:
                    self.active_trade.ticket = p.ticket
                self.active_trade.entry_price = p.price_open

            self.active_trade.pending_order_tickets = still_pending

            if not still_pending:
                self.active_trade.is_pending = False
                self.active_trade.order_ticket = None
                if self.active_trade.sub_tickets:
                    self.active_trade.remaining_lot = round(
                        sum(p.volume for p in positions if p.ticket in set(self.active_trade.sub_tickets)), 2)
                    self.active_trade.lot_size = self.active_trade.remaining_lot
                    log.info("Reconcile: pending orders resolved — %d position(s) active",
                             len(self.active_trade.sub_tickets))
                    self._save_state()
                    return
                else:
                    log.info("Reconcile: all pending orders gone, no positions — clearing")
                    self.active_trade = None
                    self._save_state()
                    return
            else:
                log.info("Reconcile: %d pending order(s) still active, %d filled",
                         len(still_pending), len(new_pos))
                self._save_state()
                return

        if has_state and has_position:
            # Check if any of our tracked sub-positions are still open
            open_mt5 = {p.ticket for p in positions}
            sub_tickets = self.active_trade.sub_tickets or [self.active_trade.ticket]
            still_open = [t for t in sub_tickets if t in open_mt5]

            if still_open:
                # Update state to reflect current reality
                closed_count = len(sub_tickets) - len(still_open)
                if closed_count > self.active_trade.tp_hits_count:
                    self.active_trade.tp_hits_count = closed_count
                self.active_trade.remaining_lot = round(
                    sum(p.volume for p in positions if p.ticket in set(still_open)), 2)
                self._save_state()
                log.info("Reconcile: %d/%d sub-positions still open — resuming (tp_hits=%d)",
                         len(still_open), len(sub_tickets), self.active_trade.tp_hits_count)
            else:
                log.warning("Reconcile: none of tracked tickets found in MT5 — clearing state")
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
            # Remove unknown keys that may have been saved by older versions
            valid_fields = {f.name for f in __import__('dataclasses').fields(TradeState)}
            trade_data = {k: v for k, v in trade_data.items() if k in valid_fields}
            self.active_trade = TradeState(**trade_data)
            # Backward compat: old state files won't have remaining_lot or sub_tickets
            if self.active_trade.remaining_lot <= 0:
                self.active_trade.remaining_lot = self.active_trade.lot_size
            if not self.active_trade.sub_tickets and not self.active_trade.is_pending:
                self.active_trade.sub_tickets = [self.active_trade.ticket]
            if not hasattr(self.active_trade, 'pending_order_tickets'):
                self.active_trade.pending_order_tickets = []
            log.info("Loaded active trade: tickets=%s, pending=%s, remaining_lot=%.2f, tp_hits=%d",
                     self.active_trade.sub_tickets, self.active_trade.pending_order_tickets,
                     self.active_trade.remaining_lot, self.active_trade.tp_hits_count)

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

    async def _modify_sl_all(self, new_sl: float) -> bool:
        """Modify SL on all open sub-positions. Returns True if all succeeded."""
        trade = self.active_trade
        if trade is None:
            return False

        sub_tickets = trade.sub_tickets or [trade.ticket]

        positions = await self.mt5.get_open_positions_async()
        open_mt5 = {p.ticket for p in positions}

        all_ok = True
        modified = 0
        for ticket in sub_tickets:
            if ticket in open_mt5:
                result = await self.mt5.modify_sl_async(ticket, new_sl)
                if result.success:
                    modified += 1
                else:
                    log.error("SL modify failed for ticket=%d: %s", ticket, result.error_message)
                    all_ok = False

        if modified > 0:
            log.info("SL modified on %d position(s) to %.2f", modified, new_sl)
        return all_ok

    async def _close_all_positions(self) -> tuple[int, int]:
        """Close all open sub-positions. Returns (closed_count, failed_count)."""
        trade = self.active_trade
        if trade is None:
            return 0, 0

        sub_tickets = trade.sub_tickets or [trade.ticket]

        positions = await self.mt5.get_open_positions_async()
        open_mt5 = {p.ticket for p in positions}

        closed = 0
        failed = 0
        for ticket in sub_tickets:
            if ticket in open_mt5:
                result = await self.mt5.close_position_async(ticket)
                if result.success:
                    closed += 1
                else:
                    log.error("Close failed for ticket=%d: %s", ticket, result.error_message)
                    failed += 1

        return closed, failed

    def _kill_switch_active(self) -> bool:
        return os.path.exists(os.path.join(self.base_dir, KILL_SWITCH_FILE))

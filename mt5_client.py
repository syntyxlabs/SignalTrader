"""MT5 Client — Thin wrapper around MetaTrader5 with async bridge."""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import MetaTrader5 as mt5

from models import Config, Direction, TradeResult, TradeState

log = logging.getLogger("signal_trader.mt5")

KILL_SWITCH_FILE = "STOP_TRADING"


class MT5Client:
    def __init__(self, config: Config, base_dir: str):
        self.config = config
        self.base_dir = base_dir
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._connected = False

    # ── Connection ──────────────────────────────────────────────

    def connect(self) -> bool:
        """Initialize MT5 terminal and log in."""
        if not mt5.initialize():
            log.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        login = int(os.getenv("MT5_LOGIN", "0"))
        password = os.getenv("MT5_PASSWORD", "")
        server = os.getenv("MT5_SERVER", "")

        if not mt5.login(login, password=password, server=server):
            log.error("MT5 login failed: %s", mt5.last_error())
            mt5.shutdown()
            return False

        info = mt5.terminal_info()
        log.info("MT5 connected — build=%s, company=%s", info.build, info.company)

        if not info.trade_allowed:
            log.error("Algo trading is DISABLED in MT5 terminal — enable it in Tools > Options > Expert Advisors")
            mt5.shutdown()
            return False

        self._connected = True
        self._startup_health_check()
        return True

    def _startup_health_check(self) -> None:
        """Log all broker-specific settings to catch issues before first trade."""
        sym = mt5.symbol_info(self.config.mt5_symbol)
        if sym is None:
            log.error("HEALTH CHECK FAILED: Symbol %s not found!", self.config.mt5_symbol)
            return

        tick = mt5.symbol_info_tick(self.config.mt5_symbol)
        account = mt5.account_info()

        log.info("=== STARTUP HEALTH CHECK ===")
        log.info("Symbol: %s (visible=%s, trade_mode=%d)", sym.name, sym.visible, sym.trade_mode)
        log.info("Filling mode: %d (FOK=%s, IOC=%s) -> using %d",
                 sym.filling_mode,
                 bool(sym.filling_mode & 1),
                 bool(sym.filling_mode & 2),
                 self._get_filling_mode())
        log.info("Volume: min=%.2f, max=%.2f, step=%.2f (config lot=%.2f)",
                 sym.volume_min, sym.volume_max, sym.volume_step, self.config.lot_size)
        log.info("Stops level: %d points (min SL/TP distance from price)", sym.trade_stops_level)
        log.info("Freeze level: %d points (can't modify when price within this)", sym.trade_freeze_level)
        log.info("Spread: %d points (%.2f price units)", sym.spread, sym.spread * sym.point)
        log.info("Contract size: %.0f, Tick size: %.5f, Tick value: %.2f",
                 sym.trade_contract_size, sym.trade_tick_size, sym.trade_tick_value)
        if tick:
            log.info("Current price: bid=%.2f, ask=%.2f", tick.bid, tick.ask)
        if account:
            log.info("Account: balance=%.2f, equity=%.2f, margin_free=%.2f, leverage=1:%d",
                     account.balance, account.equity, account.margin_free, account.leverage)
            log.info("Account type: %s", "Hedging" if account.margin_mode == 2 else "Netting")
        log.info("=== END HEALTH CHECK ===")

        # Validate config against broker
        if self.config.lot_size < sym.volume_min:
            log.error("Config lot_size (%.2f) < broker minimum (%.2f)!", self.config.lot_size, sym.volume_min)
        if round(self.config.lot_size % sym.volume_step, 8) != 0:
            log.warning("Config lot_size (%.2f) not aligned with volume_step (%.2f)",
                        self.config.lot_size, sym.volume_step)

    def disconnect(self) -> None:
        mt5.shutdown()
        self._connected = False
        log.info("MT5 disconnected")

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        info = mt5.terminal_info()
        return info is not None and info.connected

    # ── Safety checks ───────────────────────────────────────────

    def _check_kill_switch(self) -> bool:
        """Return True if kill switch is active (should NOT trade)."""
        return os.path.exists(os.path.join(self.base_dir, KILL_SWITCH_FILE))

    def _enforce_lot_cap(self, lot: float) -> float:
        """Clamp lot size to absolute max."""
        capped = min(lot, self.config.max_lot, self.config.ABSOLUTE_MAX_LOT)
        if capped != lot:
            log.warning("Lot size clamped: %.2f -> %.2f", lot, capped)
        return capped

    def _check_symbol(self) -> bool:
        """Verify the symbol exists and is tradeable."""
        info = mt5.symbol_info(self.config.mt5_symbol)
        if info is None:
            log.error("Symbol %s not found", self.config.mt5_symbol)
            return False
        if not info.visible:
            mt5.symbol_select(self.config.mt5_symbol, True)
        if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            log.error("Symbol %s not fully tradeable (trade_mode=%d)", self.config.mt5_symbol, info.trade_mode)
            return False
        return True

    def _get_filling_mode(self) -> int:
        """Detect the supported filling mode for the symbol.
        filling_mode bitmask: bit 0 (1) = FOK, bit 1 (2) = IOC.
        """
        info = mt5.symbol_info(self.config.mt5_symbol)
        if info is None:
            return mt5.ORDER_FILLING_RETURN
        filling = info.filling_mode
        if filling & 1:  # FOK supported
            return mt5.ORDER_FILLING_FOK
        if filling & 2:  # IOC supported
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    # ── Sync order methods ──────────────────────────────────────

    def open_position(self, direction: Direction, lot: float, sl: float, tp_distance: float = 0.0, tp_price: float = 0.0) -> TradeResult:
        """Open a market order.

        Args:
            tp_distance: Fixed TP distance from tick price (0=disabled).
            tp_price: Absolute TP price (0=disabled). Takes priority over tp_distance.
        """
        if self._check_kill_switch():
            return TradeResult(success=False, error_message="Kill switch active")

        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if not self._check_symbol():
            return TradeResult(success=False, error_message=f"Symbol {self.config.mt5_symbol} unavailable")

        lot = self._enforce_lot_cap(lot)

        if self.config.dry_run:
            tp_info = f" TP={tp_price:.2f}" if tp_price > 0 else ""
            log.info("[DRY-RUN] Would open %s %.2f %s SL=%.2f%s",
                     direction.value, lot, self.config.mt5_symbol, sl, tp_info)
            return TradeResult(success=True, ticket=0, price=0.0)

        order_type = mt5.ORDER_TYPE_BUY if direction == Direction.BUY else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(self.config.mt5_symbol)
        if tick is None:
            return TradeResult(success=False, error_message="Cannot get tick data")

        price = tick.ask if direction == Direction.BUY else tick.bid

        # Determine TP: absolute price takes priority, then distance-based
        tp = 0.0
        if tp_price > 0:
            tp = tp_price
        elif tp_distance > 0:
            tp = price + tp_distance if direction == Direction.BUY else price - tp_distance

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.config.mt5_symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "deviation": 20,
            "magic": 123456,
            "comment": "SignalTrader",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }
        if tp > 0:
            request["tp"] = tp

        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Order failed: retcode=%d, comment=%s", result.retcode, result.comment)
            return TradeResult(
                success=False,
                error_code=result.retcode,
                error_message=result.comment,
            )

        # Some brokers return price=0 from order_send — fetch real fill from position
        fill_price = result.price
        if not fill_price:
            pos = self._get_position_by_ticket(result.order)
            if pos:
                fill_price = pos.price_open
                log.info("Fetched real fill price from position: %.2f", fill_price)

        log.info("Position opened: ticket=%d, price=%.2f, lot=%.2f", result.order, fill_price, lot)
        return TradeResult(success=True, ticket=result.order, price=fill_price)

    def modify_sl(self, ticket: int, new_sl: float) -> TradeResult:
        """Modify the stop loss of an open position."""
        if self._check_kill_switch():
            return TradeResult(success=False, error_message="Kill switch active")

        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if self.config.dry_run:
            log.info("[DRY-RUN] Would modify ticket=%d SL=%.2f", ticket, new_sl)
            return TradeResult(success=True, ticket=ticket)

        # Get current position to preserve TP
        position = self._get_position_by_ticket(ticket)
        if position is None:
            return TradeResult(success=False, error_message=f"Position {ticket} not found")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.config.mt5_symbol,
            "position": ticket,
            "sl": new_sl,
            "tp": position.tp,
        }

        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("SL modify failed: retcode=%d, comment=%s", result.retcode, result.comment)
            return TradeResult(success=False, error_code=result.retcode, error_message=result.comment)

        log.info("SL modified: ticket=%d, new_sl=%.2f", ticket, new_sl)
        return TradeResult(success=True, ticket=ticket)

    def modify_tp(self, ticket: int, new_tp: float) -> TradeResult:
        """Modify the take profit of an open position."""
        if self._check_kill_switch():
            return TradeResult(success=False, error_message="Kill switch active")

        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if self.config.dry_run:
            log.info("[DRY-RUN] Would modify ticket=%d TP=%.2f", ticket, new_tp)
            return TradeResult(success=True, ticket=ticket)

        # Get current position to preserve SL
        position = self._get_position_by_ticket(ticket)
        if position is None:
            return TradeResult(success=False, error_message=f"Position {ticket} not found")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.config.mt5_symbol,
            "position": ticket,
            "sl": position.sl,
            "tp": new_tp,
        }

        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("TP modify failed: retcode=%d, comment=%s", result.retcode, result.comment)
            return TradeResult(success=False, error_code=result.retcode, error_message=result.comment)

        log.info("TP modified: ticket=%d, new_tp=%.2f", ticket, new_tp)
        return TradeResult(success=True, ticket=ticket)

    def close_position(self, ticket: int, volume: float = 0.0) -> TradeResult:
        """Close a position (fully or partially) at market.

        Args:
            ticket: Position ticket to close.
            volume: Lot size to close. If 0 or >= position volume, close entire position.
        """
        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if self.config.dry_run:
            vol_str = f" vol={volume:.2f}" if volume > 0 else ""
            log.info("[DRY-RUN] Would close ticket=%d%s", ticket, vol_str)
            return TradeResult(success=True, ticket=ticket)

        position = self._get_position_by_ticket(ticket)
        if position is None:
            return TradeResult(success=False, error_message=f"Position {ticket} not found")

        # Determine close volume: partial or full
        close_volume = position.volume
        if 0 < volume < position.volume:
            close_volume = volume

        # Reverse the direction to close
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(self.config.mt5_symbol)
        if tick is None:
            return TradeResult(success=False, error_message="Cannot get tick data")

        price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.config.mt5_symbol,
            "volume": close_volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 123456,
            "comment": "SignalTrader close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }

        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Close failed: retcode=%d, comment=%s", result.retcode, result.comment)
            return TradeResult(success=False, error_code=result.retcode, error_message=result.comment)

        partial = close_volume < position.volume
        log.info("Position %s: ticket=%d, vol=%.2f, price=%.2f",
                 "partial-closed" if partial else "closed", ticket, close_volume, result.price)
        return TradeResult(success=True, ticket=ticket, price=result.price)

    def open_limit_order(self, direction: Direction, lot: float, price: float, sl: float, tp_distance: float = 0.0, tp_price: float = 0.0) -> TradeResult:
        """Place a pending limit order at a specific price. Returns TradeResult."""
        if self._check_kill_switch():
            return TradeResult(success=False, error_message="Kill switch active")

        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if not self._check_symbol():
            return TradeResult(success=False, error_message=f"Symbol {self.config.mt5_symbol} unavailable")

        lot = self._enforce_lot_cap(lot)

        if self.config.dry_run:
            tp_info = f" TP={tp_price:.2f}" if tp_price > 0 else ""
            log.info("[DRY-RUN] Would place %s LIMIT %.2f %s @ %.2f SL=%.2f%s",
                     direction.value, lot, self.config.mt5_symbol, price, sl, tp_info)
            return TradeResult(success=True, ticket=0, price=price)

        order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == Direction.BUY else mt5.ORDER_TYPE_SELL_LIMIT

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.config.mt5_symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "deviation": 20,
            "magic": 123456,
            "comment": "SignalTrader limit",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }
        # Determine TP: absolute price takes priority, then distance-based
        tp = 0.0
        if tp_price > 0:
            tp = tp_price
        elif tp_distance > 0:
            tp = price + tp_distance if direction == Direction.BUY else price - tp_distance
        if tp > 0:
            request["tp"] = tp

        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Limit order failed: retcode=%d, comment=%s", result.retcode, result.comment)
            return TradeResult(
                success=False,
                error_code=result.retcode,
                error_message=result.comment,
            )

        log.info("Limit order placed: order=%d, %s @ %.2f, lot=%.2f", result.order, direction.value, price, lot)
        return TradeResult(success=True, ticket=result.order, price=price)

    def cancel_order(self, order_ticket: int) -> TradeResult:
        """Cancel a pending order."""
        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if self.config.dry_run:
            log.info("[DRY-RUN] Would cancel order=%d", order_ticket)
            return TradeResult(success=True, ticket=order_ticket)

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": order_ticket,
        }

        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Cancel order failed: retcode=%d, comment=%s", result.retcode, result.comment)
            return TradeResult(success=False, error_code=result.retcode, error_message=result.comment)

        log.info("Order cancelled: order=%d", order_ticket)
        return TradeResult(success=True, ticket=order_ticket)

    # ── Query methods ───────────────────────────────────────────

    def get_open_positions(self, symbol: str = None) -> list:
        """Get open positions, optionally filtered by symbol."""
        if not self.is_connected():
            return []
        symbol = symbol or self.config.mt5_symbol
        positions = mt5.positions_get(symbol=symbol)
        return list(positions) if positions else []

    def get_current_price(self) -> Optional[float]:
        """Get the current bid price for the configured symbol."""
        if not self.is_connected():
            return None
        tick = mt5.symbol_info_tick(self.config.mt5_symbol)
        if tick is None:
            return None
        return tick.bid

    def get_pending_orders(self, symbol: str = None) -> list:
        """Get pending orders, optionally filtered by symbol."""
        if not self.is_connected():
            return []
        symbol = symbol or self.config.mt5_symbol
        orders = mt5.orders_get(symbol=symbol)
        return list(orders) if orders else []

    def _get_position_by_ticket(self, ticket: int):
        """Get a specific position by ticket number."""
        positions = mt5.positions_get(ticket=ticket)
        if positions and len(positions) > 0:
            return positions[0]
        return None

    # ── Async bridge ────────────────────────────────────────────

    async def connect_async(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.connect)

    async def open_position_async(self, direction: Direction, lot: float, sl: float, tp_distance: float = 0.0, tp_price: float = 0.0) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.open_position, direction, lot, sl, tp_distance, tp_price)

    async def modify_sl_async(self, ticket: int, new_sl: float) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.modify_sl, ticket, new_sl)

    async def modify_tp_async(self, ticket: int, new_tp: float) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.modify_tp, ticket, new_tp)

    async def close_position_async(self, ticket: int, volume: float = 0.0) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.close_position, ticket, volume)

    async def get_open_positions_async(self, symbol: str = None) -> list:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.get_open_positions, symbol)

    async def open_limit_order_async(self, direction: Direction, lot: float, price: float, sl: float, tp_distance: float = 0.0, tp_price: float = 0.0) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.open_limit_order, direction, lot, price, sl, tp_distance, tp_price)

    async def cancel_order_async(self, order_ticket: int) -> TradeResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.cancel_order, order_ticket)

    async def get_pending_orders_async(self, symbol: str = None) -> list:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.get_pending_orders, symbol)

    async def get_current_price_async(self) -> Optional[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.get_current_price)

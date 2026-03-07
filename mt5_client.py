"""MT5 Client — Thin wrapper around MetaTrader5 with async bridge."""

import asyncio
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

import MetaTrader5 as mt5

from models import Config, Direction, TradeResult, TradeState

log = logging.getLogger("signal_trader.mt5")

KILL_SWITCH_FILE = "STOP_TRADING"
MAGIC_NUMBER = 123456
MT5_TIMEOUT = 30  # seconds — prevents frozen terminal from blocking forever


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

    def _pre_trade_checks(self, check_symbol: bool = False, cap_lot: float = 0.0) -> Optional[TradeResult]:
        """Common pre-trade validation. Returns TradeResult on failure, None if OK."""
        if os.path.exists(os.path.join(self.base_dir, KILL_SWITCH_FILE)):
            return TradeResult(success=False, error_message="Kill switch active")
        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")
        if check_symbol and not self._check_symbol():
            return TradeResult(success=False, error_message=f"Symbol {self.config.mt5_symbol} unavailable")
        return None

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
        """Detect the supported filling mode for the symbol."""
        info = mt5.symbol_info(self.config.mt5_symbol)
        if info is None:
            return mt5.ORDER_FILLING_RETURN
        filling = info.filling_mode
        if filling & 1:
            return mt5.ORDER_FILLING_FOK
        if filling & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _calculate_tp(self, direction: Direction, base_price: float,
                      tp_distance: float = 0.0, tp_price: float = 0.0) -> float:
        """Calculate TP price. Absolute tp_price takes priority over distance."""
        if tp_price > 0:
            return tp_price
        if tp_distance > 0:
            return base_price + tp_distance if direction == Direction.BUY else base_price - tp_distance
        return 0.0

    def _send_order(self, request: dict, description: str) -> TradeResult:
        """Send order to MT5 and handle common error patterns."""
        result = mt5.order_send(request)
        if result is None:
            return TradeResult(success=False, error_message=f"order_send returned None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("%s failed: retcode=%d, comment=%s", description, result.retcode, result.comment)
            return TradeResult(success=False, error_code=result.retcode, error_message=result.comment)
        return TradeResult(success=True, ticket=result.order, price=result.price)

    # ── Sync order methods ──────────────────────────────────────

    def open_position(self, direction: Direction, lot: float, sl: float,
                      tp_distance: float = 0.0, tp_price: float = 0.0) -> TradeResult:
        """Open a market order."""
        fail = self._pre_trade_checks(check_symbol=True)
        if fail:
            return fail

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
        tp = self._calculate_tp(direction, price, tp_distance, tp_price)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.config.mt5_symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": "SignalTrader",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }
        if tp > 0:
            request["tp"] = tp

        result = self._send_order(request, "Order")
        if not result.success:
            return result

        # Some brokers return price=0 from order_send — fetch real fill from position
        fill_price = result.price
        if not fill_price:
            pos = self._get_position_by_ticket(result.ticket)
            if pos:
                fill_price = pos.price_open
                log.info("Fetched real fill price from position: %.2f", fill_price)

        log.info("Position opened: ticket=%d, price=%.2f, lot=%.2f", result.ticket, fill_price, lot)
        return TradeResult(success=True, ticket=result.ticket, price=fill_price)

    def modify_sltp(self, ticket: int, sl: float = None, tp: float = None) -> TradeResult:
        """Modify SL and/or TP of an open position. Preserves the other value if not specified."""
        fail = self._pre_trade_checks()
        if fail:
            return fail

        if self.config.dry_run:
            parts = []
            if sl is not None:
                parts.append(f"SL={sl:.2f}")
            if tp is not None:
                parts.append(f"TP={tp:.2f}")
            log.info("[DRY-RUN] Would modify ticket=%d %s", ticket, " ".join(parts))
            return TradeResult(success=True, ticket=ticket)

        position = self._get_position_by_ticket(ticket)
        if position is None:
            return TradeResult(success=False, error_message=f"Position {ticket} not found")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.config.mt5_symbol,
            "position": ticket,
            "sl": sl if sl is not None else position.sl,
            "tp": tp if tp is not None else position.tp,
        }

        result = self._send_order(request, f"SLTP modify (ticket={ticket})")
        if result.success:
            parts = []
            if sl is not None:
                parts.append(f"sl={sl:.2f}")
            if tp is not None:
                parts.append(f"tp={tp:.2f}")
            log.info("SLTP modified: ticket=%d, %s", ticket, ", ".join(parts))
            result.ticket = ticket
        return result

    def modify_sl(self, ticket: int, new_sl: float) -> TradeResult:
        """Modify the stop loss of an open position."""
        return self.modify_sltp(ticket, sl=new_sl)

    def modify_tp(self, ticket: int, new_tp: float) -> TradeResult:
        """Modify the take profit of an open position."""
        return self.modify_sltp(ticket, tp=new_tp)

    def close_position(self, ticket: int, volume: float = 0.0) -> TradeResult:
        """Close a position (fully or partially) at market."""
        if not self.is_connected():
            return TradeResult(success=False, error_message="MT5 not connected")

        if self.config.dry_run:
            vol_str = f" vol={volume:.2f}" if volume > 0 else ""
            log.info("[DRY-RUN] Would close ticket=%d%s", ticket, vol_str)
            return TradeResult(success=True, ticket=ticket)

        position = self._get_position_by_ticket(ticket)
        if position is None:
            return TradeResult(success=False, error_message=f"Position {ticket} not found")

        close_volume = position.volume
        if 0 < volume < position.volume:
            close_volume = volume

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
            "magic": MAGIC_NUMBER,
            "comment": "SignalTrader close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }

        result = self._send_order(request, f"Close (ticket={ticket})")
        if result.success:
            partial = close_volume < position.volume
            log.info("Position %s: ticket=%d, vol=%.2f, price=%.2f",
                     "partial-closed" if partial else "closed", ticket, close_volume, result.price)
            result.ticket = ticket
        return result

    def open_limit_order(self, direction: Direction, lot: float, price: float,
                         sl: float, tp_distance: float = 0.0, tp_price: float = 0.0) -> TradeResult:
        """Place a pending limit order at a specific price."""
        fail = self._pre_trade_checks(check_symbol=True)
        if fail:
            return fail

        lot = self._enforce_lot_cap(lot)

        if self.config.dry_run:
            tp_info = f" TP={tp_price:.2f}" if tp_price > 0 else ""
            log.info("[DRY-RUN] Would place %s LIMIT %.2f %s @ %.2f SL=%.2f%s",
                     direction.value, lot, self.config.mt5_symbol, price, sl, tp_info)
            return TradeResult(success=True, ticket=0, price=price)

        order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == Direction.BUY else mt5.ORDER_TYPE_SELL_LIMIT
        tp = self._calculate_tp(direction, price, tp_distance, tp_price)

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.config.mt5_symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": "SignalTrader limit",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }
        if tp > 0:
            request["tp"] = tp

        result = self._send_order(request, "Limit order")
        if result.success:
            log.info("Limit order placed: order=%d, %s @ %.2f, lot=%.2f",
                     result.ticket, direction.value, price, lot)
        return result

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

        result = self._send_order(request, f"Cancel order (ticket={order_ticket})")
        if result.success:
            log.info("Order cancelled: order=%d", order_ticket)
            result.ticket = order_ticket
        return result

    # ── Query methods ───────────────────────────────────────────

    def get_open_positions(self, symbol: str = None) -> list:
        """Get open positions, optionally filtered by symbol."""
        if not self.is_connected():
            return []
        symbol = symbol or self.config.mt5_symbol
        positions = mt5.positions_get(symbol=symbol)
        return list(positions) if positions else []

    def get_current_price(self, direction: Direction = None) -> Optional[float]:
        """Get the current price. Returns ask for BUY, bid for SELL/None."""
        if not self.is_connected():
            return None
        tick = mt5.symbol_info_tick(self.config.mt5_symbol)
        if tick is None:
            return None
        if direction == Direction.BUY:
            return tick.ask
        return tick.bid

    def get_pending_orders(self, symbol: str = None) -> list:
        """Get pending orders, optionally filtered by symbol."""
        if not self.is_connected():
            return []
        symbol = symbol or self.config.mt5_symbol
        orders = mt5.orders_get(symbol=symbol)
        return list(orders) if orders else []

    def get_position_close_reason(self, ticket: int) -> Optional[str]:
        """Check deal history to determine why a position was closed.

        Returns 'TP', 'SL', 'SO', 'MANUAL', 'EA', or None if unknown.
        """
        if not self.is_connected():
            return None

        # Some brokers need a time range for history_deals_get to work
        date_from = datetime.now() - timedelta(days=7)
        date_to = datetime.now() + timedelta(hours=1)
        # Try with position filter first, fall back to time range
        deals = mt5.history_deals_get(position=ticket)
        if not deals:
            all_deals = mt5.history_deals_get(date_from, date_to)
            if all_deals:
                deals = [d for d in all_deals if d.position_id == ticket]
        if not deals:
            return None

        # Find the closing deal (entry == DEAL_ENTRY_OUT = 1)
        for deal in deals:
            if deal.entry == 1:  # DEAL_ENTRY_OUT
                reason_map = {
                    0: "MANUAL",   # DEAL_REASON_CLIENT
                    3: "EA",       # DEAL_REASON_EXPERT
                    4: "SL",       # DEAL_REASON_SL
                    5: "TP",       # DEAL_REASON_TP
                    6: "SO",       # DEAL_REASON_SO
                }
                return reason_map.get(deal.reason, f"REASON_{deal.reason}")

        return None

    def _get_position_by_ticket(self, ticket: int):
        """Get a specific position by ticket number."""
        positions = mt5.positions_get(ticket=ticket)
        if positions and len(positions) > 0:
            return positions[0]
        return None

    # ── Async bridge ────────────────────────────────────────────

    async def _run(self, func, *args):
        """Run a sync MT5 method in the executor with timeout protection."""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(self._executor, functools.partial(func, *args)),
            timeout=MT5_TIMEOUT,
        )

    async def connect_async(self) -> bool:
        return await self._run(self.connect)

    async def open_position_async(self, direction, lot, sl, tp_distance=0.0, tp_price=0.0):
        return await self._run(self.open_position, direction, lot, sl, tp_distance, tp_price)

    async def modify_sl_async(self, ticket, new_sl):
        return await self._run(self.modify_sl, ticket, new_sl)

    async def modify_tp_async(self, ticket, new_tp):
        return await self._run(self.modify_tp, ticket, new_tp)

    async def modify_sltp_async(self, ticket, sl=None, tp=None):
        return await self._run(self.modify_sltp, ticket, sl, tp)

    async def close_position_async(self, ticket, volume=0.0):
        return await self._run(self.close_position, ticket, volume)

    async def get_open_positions_async(self, symbol=None):
        return await self._run(self.get_open_positions, symbol)

    async def open_limit_order_async(self, direction, lot, price, sl, tp_distance=0.0, tp_price=0.0):
        return await self._run(self.open_limit_order, direction, lot, price, sl, tp_distance, tp_price)

    async def cancel_order_async(self, order_ticket):
        return await self._run(self.cancel_order, order_ticket)

    async def get_pending_orders_async(self, symbol=None):
        return await self._run(self.get_pending_orders, symbol)

    async def get_current_price_async(self, direction=None):
        return await self._run(self.get_current_price, direction)

    async def get_position_close_reason_async(self, ticket):
        return await self._run(self.get_position_close_reason, ticket)

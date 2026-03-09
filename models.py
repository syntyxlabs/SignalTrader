"""Shared data models for Signal Trader."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalType(str, Enum):
    NEW_SIGNAL = "NEW_SIGNAL"
    SL_UPDATE = "SL_UPDATE"
    TP_HIT = "TP_HIT"
    CLOSE_SIGNAL = "CLOSE_SIGNAL"
    TRAIL_STOP = "TRAIL_STOP"
    NOISE = "NOISE"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderExecution(str, Enum):
    MARKET = "MARKET"  # Execute immediately at current price
    LIMIT = "LIMIT"    # Pending order at specified price


@dataclass
class ParsedSignal:
    type: SignalType
    raw_message: str
    timestamp: float  # Telegram server timestamp (UTC)
    direction: Optional[Direction] = None
    pair: Optional[str] = None
    price: Optional[float] = None
    price_low: Optional[float] = None  # Lower bound of entry zone (e.g., "PRICE 5025 - 5020" → 5020)
    sl: Optional[float] = None
    tp: list[float] = field(default_factory=list)
    new_sl: Optional[float] = None
    reason: Optional[str] = None
    tp_number: Optional[int] = None
    trail_distance: Optional[float] = None  # Trail distance in price units (e.g., 5.0 = $5)
    execution: OrderExecution = OrderExecution.MARKET  # Market or limit order


@dataclass
class TradeState:
    ticket: int
    direction: Direction
    pair: str
    entry_price: float
    signal_price: float
    current_sl: float
    tp_levels: list[float]
    lot_size: float
    opened_at: float  # Unix timestamp
    last_updated: float  # Unix timestamp
    is_pending: bool = False  # True if this is a pending limit order
    order_ticket: Optional[int] = None  # MT5 order ticket (for pending orders)
    trail_active: bool = False  # True when trailing stop is activated
    trail_distance: float = 0.0  # Trail distance in price units
    trail_price: float = 0.0  # Best price seen since trail activated (high for BUY, low for SELL)
    tp_upgraded: bool = False  # True after TP was upgraded from fixed to signal TP2
    tp_hits_count: int = 0  # How many TPs have been closed (positions auto-closed by MT5)
    remaining_lot: float = 0.0  # Total lot still open across all sub-positions
    sub_tickets: list[int] = field(default_factory=list)  # All position tickets (one per TP level)
    pending_order_tickets: list[int] = field(default_factory=list)  # Pending limit order tickets
    closed_tickets: list[int] = field(default_factory=list)  # Tickets confirmed closed (prevents re-detection)
    pending_sl: Optional[float] = None  # SL that failed to apply — retry on next poll


@dataclass
class TradeResult:
    success: bool
    ticket: Optional[int] = None
    error_code: Optional[int] = None
    error_message: Optional[str] = None
    price: Optional[float] = None


@dataclass
class ChannelConfig:
    channel_id: int
    channel_name: str
    lot_size: Optional[float] = None  # Per-channel override (None = use global)
    close_lot_per_tp: Optional[float] = None  # Per-channel override (None = use global)


@dataclass
class Config:
    # Channels
    channels: list[ChannelConfig] = field(default_factory=list)
    max_positions: int = 2  # Global cap across all channels
    # Trading
    pair: str = "XAUUSD"
    mt5_symbol: str = "XAUUSD.."
    lot_size: float = 0.01
    max_lot: float = 0.01
    trading_enabled: bool = False
    dry_run: bool = True
    # Safety
    max_open_trades: int = 1
    stale_signal_seconds: int = 60
    stale_edit_seconds: int = 600  # Max age of original message for edits (10min — provider edits in details over 1-7min)
    position_poll_interval: int = 30
    max_price_deviation: float = 10.0
    max_sl_distance: float = 20.0
    default_sl_distance: float = 10.0  # Auto-SL distance when signal has no SL
    default_trail_distance: float = 5.0  # Trailing stop distance in price units ($5 = 50 pips for gold)
    # Fixed TP — set to 0 to disable and use provider's TPs instead
    fixed_tp_distance: float = 0.0  # Fixed TP distance from entry (0 = disabled, use signal TPs)
    close_lot_per_tp: float = 0.01  # Lot to partial-close at each TP hit
    # Notifications
    notify_method: str = "saved_messages"
    notify_enabled: bool = True

    ABSOLUTE_MAX_LOT: float = 0.05  # Hardcoded safety cap

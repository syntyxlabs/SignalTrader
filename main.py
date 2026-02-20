"""Signal Trader — Entry point. Wires all components and runs the event loop."""

import asyncio
import json
import logging
import os
import signal
import sys

from dotenv import load_dotenv

from models import Config
from mt5_client import MT5Client
from parser import SignalParser
from trade_manager import TradeManager
from channel_listener import ChannelListener

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "trades.log")),
    ],
)
log = logging.getLogger("signal_trader")


def load_config(path: str = None) -> Config:
    """Load config.json and environment variables into a Config dataclass."""
    if path is None:
        path = os.path.join(BASE_DIR, "config.json")

    with open(path) as f:
        raw = json.load(f)

    cfg = Config(
        channel_id=raw["channel"]["id"],
        channel_name=raw["channel"]["name"],
        pair=raw["trading"]["pair"],
        mt5_symbol=raw["trading"]["mt5_symbol"],
        lot_size=raw["trading"]["lot_size"],
        max_lot=raw["trading"]["max_lot"],
        trading_enabled=raw["trading"]["enabled"],
        dry_run=raw["trading"].get("dry_run", True),
        max_open_trades=raw["safety"]["max_open_trades"],
        stale_signal_seconds=raw["safety"]["stale_signal_seconds"],
        position_poll_interval=raw["safety"]["position_poll_interval"],
        max_price_deviation=raw["safety"].get("max_price_deviation", 10.0),
        max_sl_distance=raw["safety"].get("max_sl_distance", 20.0),
        default_sl_distance=raw["safety"].get("default_sl_distance", 10.0),
        default_trail_distance=raw["safety"].get("default_trail_distance", 5.0),
        fixed_tp_distance=raw["safety"].get("fixed_tp_distance", 0.0),
        notify_method=raw["notifications"]["method"],
        notify_enabled=raw["notifications"]["enabled"],
    )

    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Validate config values. Raises ValueError on invalid config."""
    if cfg.channel_id == 0:
        raise ValueError("channel.id must be set")
    if cfg.lot_size <= 0:
        raise ValueError("trading.lot_size must be positive")
    if cfg.lot_size > cfg.ABSOLUTE_MAX_LOT:
        raise ValueError(f"trading.lot_size ({cfg.lot_size}) exceeds ABSOLUTE_MAX_LOT ({cfg.ABSOLUTE_MAX_LOT})")
    if cfg.max_lot > cfg.ABSOLUTE_MAX_LOT:
        raise ValueError(f"trading.max_lot ({cfg.max_lot}) exceeds ABSOLUTE_MAX_LOT ({cfg.ABSOLUTE_MAX_LOT})")
    if cfg.stale_signal_seconds < 10:
        raise ValueError("safety.stale_signal_seconds too low (min 10)")
    if cfg.max_price_deviation <= 0:
        raise ValueError("safety.max_price_deviation must be positive")
    if cfg.max_sl_distance <= 0:
        raise ValueError("safety.max_sl_distance must be positive")

    required_env = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    log.info("Config loaded — pair=%s, lot=%.2f, dry_run=%s, channel=%s",
             cfg.pair, cfg.lot_size, cfg.dry_run, cfg.channel_name)


async def position_poll_loop(trade_manager: TradeManager, listener: ChannelListener, interval: int):
    """Periodically check if our tracked position is still open."""
    while True:
        try:
            await asyncio.sleep(interval)
            notification = await trade_manager.check_position_status()
            if notification:
                await listener.send_notification(notification)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Position poll error: %s", e, exc_info=True)


async def async_main() -> None:
    """Async entry point — wire components and start event loop."""
    cfg = load_config()

    if cfg.dry_run:
        log.warning("DRY-RUN MODE — no real trades will be executed")

    # Initialize MT5
    mt5_client = MT5Client(cfg, BASE_DIR)
    connected = await mt5_client.connect_async()
    if not connected:
        log.error("Failed to connect to MT5 — exiting")
        sys.exit(1)

    # Initialize components
    signal_parser = SignalParser()
    trade_manager = TradeManager(cfg, mt5_client, BASE_DIR)

    # Reconcile state with MT5 on startup
    await trade_manager.reconcile()

    # Initialize Telegram listener
    listener = ChannelListener(cfg, signal_parser, trade_manager, BASE_DIR,
                               notify_callback=None)  # Set callback after init
    listener.notify = listener.send_notification

    await listener.start()

    # Start position polling
    poll_task = asyncio.create_task(
        position_poll_loop(trade_manager, listener, cfg.position_poll_interval)
    )

    log.info("Signal Trader running. Press Ctrl+C to stop.")

    # Send startup notification
    status = "DRY-RUN" if cfg.dry_run else "LIVE"
    await listener.send_notification(
        f"Signal Trader started [{status}]\n"
        f"Pair: {cfg.pair} | Lot: {cfg.lot_size}\n"
        f"Listening to: {cfg.channel_name}"
    )

    # Run until disconnected or interrupted
    try:
        await listener.client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        log.info("Shutting down...")
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await listener.stop()
        mt5_client.disconnect()
        log.info("Signal Trader stopped.")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")


if __name__ == "__main__":
    main()

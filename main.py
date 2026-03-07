"""Signal Trader — Entry point. Wires all components and runs the event loop."""

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys

from dotenv import load_dotenv

from models import ChannelConfig, Config
from mt5_client import MT5Client
from parser import SignalParser
from trade_manager import PositionCounter, TradeManager
from channel_listener import ChannelListener
from bot import SignalTraderBot

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)),
        logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "trades.log"), encoding="utf-8",
            maxBytes=500_000, backupCount=3),
    ],
)
log = logging.getLogger("signal_trader")


def load_config(path: str = None) -> Config:
    """Load config.json and environment variables into a Config dataclass."""
    if path is None:
        path = os.path.join(BASE_DIR, "config.json")

    with open(path) as f:
        raw = json.load(f)

    # Backward compat: old "channel" key -> new "channels" list
    if "channels" in raw:
        channels = [ChannelConfig(ch["id"], ch["name"]) for ch in raw["channels"]]
    elif "channel" in raw:
        ch = raw["channel"]
        channels = [ChannelConfig(ch["id"], ch["name"])]
    else:
        raise ValueError("Config must have 'channels' or 'channel' key")

    cfg = Config(
        channels=channels,
        pair=raw["trading"]["pair"],
        mt5_symbol=raw["trading"]["mt5_symbol"],
        lot_size=raw["trading"]["lot_size"],
        max_lot=raw["trading"]["max_lot"],
        trading_enabled=raw["trading"]["enabled"],
        dry_run=raw["trading"].get("dry_run", True),
        max_positions=raw["safety"].get("max_positions", 2),
        max_open_trades=raw["safety"]["max_open_trades"],
        stale_signal_seconds=raw["safety"]["stale_signal_seconds"],
        stale_edit_seconds=raw["safety"].get("stale_edit_seconds", 600),
        position_poll_interval=raw["safety"]["position_poll_interval"],
        max_price_deviation=raw["safety"].get("max_price_deviation", 10.0),
        max_sl_distance=raw["safety"].get("max_sl_distance", 20.0),
        default_sl_distance=raw["safety"].get("default_sl_distance", 10.0),
        default_trail_distance=raw["safety"].get("default_trail_distance", 5.0),
        fixed_tp_distance=raw["safety"].get("fixed_tp_distance", 0.0),
        close_lot_per_tp=raw["safety"].get("close_lot_per_tp", 0.01),
        notify_method=raw["notifications"]["method"],
        notify_enabled=raw["notifications"]["enabled"],
    )

    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Validate config values. Raises ValueError on invalid config."""
    if not cfg.channels:
        raise ValueError("At least one channel must be configured")
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
    if cfg.max_positions < 1:
        raise ValueError("safety.max_positions must be at least 1")
    if cfg.close_lot_per_tp <= 0 and cfg.fixed_tp_distance <= 0:
        raise ValueError("safety.close_lot_per_tp must be positive when fixed_tp_distance is 0")

    required_env = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    channel_names = ", ".join(ch.channel_name for ch in cfg.channels)
    log.info("Config loaded — pair=%s, lot=%.2f, dry_run=%s, max_pos=%d, channels=[%s]",
             cfg.pair, cfg.lot_size, cfg.dry_run, cfg.max_positions, channel_names)


async def position_poll_loop(trade_managers: list[TradeManager], notify, interval: int):
    """Periodically check if tracked positions are still open (all channels)."""
    while True:
        try:
            await asyncio.sleep(interval)
            for tm in trade_managers:
                notification = await tm.check_position_status()
                if notification:
                    await notify(notification)
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

    # Initialize shared components
    signal_parser = SignalParser()
    position_counter = PositionCounter(cfg.max_positions)

    # Create per-channel TradeManagers
    trade_managers: dict[int, TradeManager] = {}
    all_managers: list[TradeManager] = []

    for ch in cfg.channels:
        # State file: state.json for single channel, state_{safe_name}.json for multi
        if len(cfg.channels) == 1:
            state_file = "state.json"
        else:
            safe_name = ch.channel_name.replace(" ", "_").replace("-", "_").lower()
            state_file = f"state_{safe_name}.json"

        tm = TradeManager(cfg, mt5_client, BASE_DIR,
                          channel_name=ch.channel_name,
                          position_counter=position_counter,
                          state_file=state_file)
        position_counter.register(tm)
        trade_managers[ch.channel_id] = tm
        all_managers.append(tm)

    # Reconcile all managers with MT5 on startup
    for tm in all_managers:
        await tm.reconcile()

    # Initialize Telegram bot (notifications + commands)
    bot = SignalTraderBot(
        api_id=int(os.getenv("TELEGRAM_API_ID")),
        api_hash=os.getenv("TELEGRAM_API_HASH"),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
    )
    bot.set_trade_managers(trade_managers)
    bot.set_mt5_client(mt5_client)
    await bot.start()

    # Initialize Telegram listener
    listener = ChannelListener(cfg, signal_parser, trade_managers, BASE_DIR,
                               notify_callback=None)
    listener.notify = bot.send  # Route all notifications through the bot

    await listener.start()

    # Start position polling (covers all channels)
    poll_task = asyncio.create_task(
        position_poll_loop(all_managers, bot.send, cfg.position_poll_interval)
    )

    log.info("Signal Trader running. Press Ctrl+C to stop.")

    # Send startup notification
    status = "DRY-RUN" if cfg.dry_run else "LIVE"
    channel_list = "\n".join(f"  - {ch.channel_name}" for ch in cfg.channels)
    await bot.send(
        f"Signal Trader started [{status}]\n"
        f"Pair: {cfg.pair} | Lot: {cfg.lot_size} | Max positions: {cfg.max_positions}\n"
        f"Channels ({len(cfg.channels)}):\n{channel_list}"
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
        await bot.stop()
        mt5_client.disconnect()
        log.info("Signal Trader stopped.")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log.info("Interrupted by user")


if __name__ == "__main__":
    main()

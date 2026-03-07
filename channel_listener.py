"""Channel Listener — Telethon client that monitors Telegram signal channels."""

import collections
import logging
import os
import time
from typing import Optional

from telethon import TelegramClient, events

from models import Config, SignalType
from parser import SignalParser
from trade_manager import TradeManager

log = logging.getLogger("signal_trader.listener")

MAX_PROCESSED_CACHE = 500  # Cap _last_processed to prevent unbounded growth


class ChannelListener:
    def __init__(self, config: Config, parser: SignalParser,
                 trade_managers: dict[int, TradeManager],
                 base_dir: str, notify_callback=None):
        self.config = config
        self.parser = parser
        self.trade_managers = trade_managers
        self.notify = notify_callback
        self.base_dir = base_dir
        self._last_processed: collections.OrderedDict[int, str] = collections.OrderedDict()
        self._signal_msg_ids: dict[int, Optional[int]] = {
            ch_id: None for ch_id in trade_managers
        }  # channel_id -> msg_id of signal that opened the trade

        api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = os.getenv("TELEGRAM_API_HASH", "")
        session_path = os.path.join(base_dir, "session")

        self.client = TelegramClient(session_path, api_id, api_hash)

    def _cache_text(self, msg_id: int, text: str) -> None:
        """Store processed text with LRU eviction."""
        self._last_processed[msg_id] = text
        while len(self._last_processed) > MAX_PROCESSED_CACHE:
            self._last_processed.popitem(last=False)

    async def start(self) -> None:
        """Start listening for new messages and edits on all channels."""
        channel_ids = list(self.trade_managers.keys())
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=channel_ids))
        self.client.add_event_handler(self._on_edit, events.MessageEdited(chats=channel_ids))
        await self.client.start()
        names = [m.channel_name for m in self.trade_managers.values()]
        log.info("Telethon connected — listening to %d channel(s): %s",
                 len(names), ", ".join(names))

    async def stop(self) -> None:
        """Disconnect the Telethon client."""
        await self.client.disconnect()
        log.info("Telethon disconnected")

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Handle a new message from a monitored channel."""
        message = event.message
        text = message.text or ""
        if not text.strip():
            return

        chat_id = event.chat_id
        trade_manager = self.trade_managers.get(chat_id)
        if trade_manager is None:
            return

        log.info("[%s] Channel message: %s", trade_manager.channel_name, text[:120])
        await self._process_signal(trade_manager, chat_id, message, text,
                                   timestamp=message.date.timestamp())

    async def _on_edit(self, event: events.MessageEdited.Event) -> None:
        """Handle an edited message — provider often edits to add price/SL/TPs."""
        message = event.message
        text = message.text or ""
        if not text.strip():
            return

        chat_id = event.chat_id
        trade_manager = self.trade_managers.get(chat_id)
        if trade_manager is None:
            return

        # Skip if text hasn't changed (Telethon fires edit for same content)
        if self._last_processed.get(message.id) == text:
            log.debug("EDIT (msg_id=%d): text unchanged — skipping", message.id)
            return

        # Block late edits to old signal messages after trade closed
        if self._signal_msg_ids.get(chat_id) == message.id and trade_manager.active_trade is None:
            log.info("EDIT (msg_id=%d): old signal msg, trade already closed — skipping", message.id)
            return

        # Block edits to old messages when no active trade (survives restart)
        original_age = time.time() - message.date.timestamp()
        if original_age > self.config.stale_edit_seconds and trade_manager.active_trade is None:
            log.info("EDIT (msg_id=%d): original message too old (%.0fs) and no active trade — skipping",
                     message.id, original_age)
            return

        log.info("[%s] Channel EDIT (msg_id=%d): %s", trade_manager.channel_name, message.id, text[:120])
        edit_time = (message.edit_date or message.date).timestamp()
        await self._process_signal(trade_manager, chat_id, message, text,
                                   timestamp=edit_time)

    async def _process_signal(self, trade_manager: TradeManager, chat_id: int,
                              message, text: str, timestamp: float) -> None:
        """Shared logic: parse message, handle signal, send notification."""
        self._cache_text(message.id, text)

        try:
            signal = await self.parser.parse(text, timestamp)

            # Stale check: if message was edited while we were parsing, discard
            if self._last_processed.get(message.id) != text:
                log.info("MSG (msg_id=%d): message changed while parsing — discarding stale result", message.id)
                return

            notification = await trade_manager.handle_signal(signal)

            # Track signal message ID only if a trade actually opened
            if signal.type == SignalType.NEW_SIGNAL and trade_manager.active_trade is not None:
                self._signal_msg_ids[chat_id] = message.id

            if notification and self.notify:
                await self.notify(notification)

        except Exception as e:
            log.error("Error processing message: %s", e, exc_info=True)
            if self.notify:
                await self.notify(f"[{trade_manager.channel_name}] Error processing signal: {e}")

    async def send_notification(self, text: str) -> None:
        """Send a notification to Saved Messages."""
        if not self.config.notify_enabled:
            return
        try:
            await self.client.send_message("me", f"\U0001f4ca Signal Trader:\n{text}")
        except Exception as e:
            log.error("Failed to send notification: %s", e)

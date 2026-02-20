"""Channel Listener — Telethon client that monitors the VIP channel."""

import logging
import os
from typing import Optional

from telethon import TelegramClient, events

from models import Config
from parser import SignalParser
from trade_manager import TradeManager

log = logging.getLogger("signal_trader.listener")


class ChannelListener:
    def __init__(self, config: Config, parser: SignalParser, trade_manager: TradeManager,
                 base_dir: str, notify_callback=None):
        self.config = config
        self.parser = parser
        self.trade_manager = trade_manager
        self.notify = notify_callback
        self.base_dir = base_dir
        self._last_processed: dict[int, str] = {}  # msg_id -> last processed text
        self._signal_msg_id: Optional[int] = None  # msg_id of the signal that opened our trade

        api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = os.getenv("TELEGRAM_API_HASH", "")
        session_path = os.path.join(base_dir, "session")

        self.client = TelegramClient(session_path, api_id, api_hash)

    async def start(self) -> None:
        """Start listening for new messages and edits."""
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=self.config.channel_id))
        self.client.add_event_handler(self._on_edit, events.MessageEdited(chats=self.config.channel_id))
        await self.client.start()
        log.info("Telethon connected — listening to channel %s (ID: %d)",
                 self.config.channel_name, self.config.channel_id)

    async def stop(self) -> None:
        """Disconnect the Telethon client."""
        await self.client.disconnect()
        log.info("Telethon disconnected")

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Handle a new message from the VIP channel."""
        message = event.message
        text = message.text or ""

        if not text.strip():
            return

        log.info("Channel message: %s", text[:120])
        self._last_processed[message.id] = text

        try:
            signal = await self.parser.parse(text, message.date.timestamp())
            notification = await self.trade_manager.handle_signal(signal)

            # Track signal message ID (even on failure, to block late edits)
            from models import SignalType
            if signal.type == SignalType.NEW_SIGNAL:
                self._signal_msg_id = message.id

            if notification and self.notify:
                await self.notify(notification)

        except Exception as e:
            log.error("Error processing message: %s", e, exc_info=True)
            if self.notify:
                await self.notify(f"Error processing signal: {e}")

    async def _on_edit(self, event: events.MessageEdited.Event) -> None:
        """Handle an edited message — provider often edits to add price/SL/TPs."""
        message = event.message
        text = message.text or ""

        if not text.strip():
            return

        # Skip if text hasn't changed (Telethon fires edit for same content)
        if self._last_processed.get(message.id) == text:
            log.debug("EDIT (msg_id=%d): text unchanged — skipping", message.id)
            return

        # Block late edits to old signal messages after trade closed
        if self._signal_msg_id == message.id and self.trade_manager.active_trade is None:
            log.info("EDIT (msg_id=%d): old signal msg, trade already closed — skipping", message.id)
            return

        log.info("Channel EDIT (msg_id=%d): %s", message.id, text[:120])
        self._last_processed[message.id] = text

        try:
            # Use edit_date for edits — provider posts "BUY NOW" first, then edits in price/TPs
            edit_time = (message.edit_date or message.date).timestamp()
            signal = await self.parser.parse(text, edit_time)
            notification = await self.trade_manager.handle_signal(signal)

            # Track signal message ID (even on failure, to block late edits)
            from models import SignalType
            if signal.type == SignalType.NEW_SIGNAL:
                self._signal_msg_id = message.id

            if notification and self.notify:
                await self.notify(notification)

        except Exception as e:
            log.error("Error processing edited message: %s", e, exc_info=True)
            if self.notify:
                await self.notify(f"Error processing edited signal: {e}")

    async def send_notification(self, text: str) -> None:
        """Send a notification to Saved Messages."""
        if not self.config.notify_enabled:
            return
        try:
            await self.client.send_message("me", f"📊 Signal Trader:\n{text}")
        except Exception as e:
            log.error("Failed to send notification: %s", e)

"""Telegram Bot — Neo SignalTrader bot for notifications and commands."""

import json
import logging
import os
from typing import Optional

from telethon import TelegramClient, events


log = logging.getLogger("signal_trader.bot")

BOT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")


def _load_chat_id() -> Optional[int]:
    if os.path.exists(BOT_STATE_FILE):
        try:
            with open(BOT_STATE_FILE) as f:
                return json.load(f).get("chat_id")
        except Exception:
            pass
    return None


def _save_chat_id(chat_id: int) -> None:
    with open(BOT_STATE_FILE, "w") as f:
        json.dump({"chat_id": chat_id}, f)


class SignalTraderBot:
    """Telegram bot — sends trade notifications and handles status commands."""

    def __init__(self, api_id: int, api_hash: str, bot_token: str):
        self.bot_token = bot_token
        self.chat_id: Optional[int] = (
            int(os.getenv("TELEGRAM_CHAT_ID", "0") or 0) or _load_chat_id()
        )
        self._trade_managers = None
        self._mt5_client = None

        self.client = TelegramClient(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_session"),
            api_id,
            api_hash,
        )

    def set_trade_managers(self, trade_managers: dict) -> None:
        self._trade_managers = trade_managers

    def set_mt5_client(self, mt5_client) -> None:
        self._mt5_client = mt5_client

    async def start(self) -> None:
        await self.client.start(bot_token=self.bot_token)
        self.client.add_event_handler(self._on_start,     events.NewMessage(pattern="/start"))
        self.client.add_event_handler(self._on_status,    events.NewMessage(pattern="/status"))
        self.client.add_event_handler(self._on_positions, events.NewMessage(pattern="/positions"))
        self.client.add_event_handler(self._on_stop,      events.NewMessage(pattern="/stop"))
        log.info("Bot started — chat_id=%s", self.chat_id or "not set (send /start)")
        if self.chat_id:
            await self.send("SignalTrader bot online. Send /status or /positions.")

    async def stop(self) -> None:
        await self.client.disconnect()

    async def send(self, text: str) -> None:
        """Send a notification to the registered chat."""
        if not self.chat_id:
            log.warning("Bot: no chat_id — send /start to register")
            return
        try:
            await self.client.send_message(self.chat_id, f"📊 {text}")
        except Exception as e:
            log.error("Bot send error: %s", e)

    # --- Command handlers ---

    async def _on_start(self, event: events.NewMessage.Event) -> None:
        self.chat_id = event.chat_id
        _save_chat_id(self.chat_id)
        sender = await event.get_sender()
        name = getattr(sender, "first_name", "?")
        log.info("Bot /start from %s (chat_id=%d)", name, self.chat_id)
        await event.reply(
            f"👋 Hey {name}! Neo SignalTrader bot ready.\n\n"
            f"Commands:\n"
            f"/status — active trades per channel\n"
            f"/positions — open MT5 positions\n"
            f"/stop — close all open positions"
        )

    async def _on_status(self, event: events.NewMessage.Event) -> None:
        if not self._trade_managers:
            await event.reply("No trade managers connected.")
            return
        lines = []
        for tm in self._trade_managers.values():
            t = tm.active_trade
            if t:
                tps = " / ".join(str(x) for x in t.tp_levels)
                lines.append(
                    f"📌 [{tm.channel_name}]\n"
                    f"  {t.direction} {t.pair} @ {t.entry_price}\n"
                    f"  SL: {t.current_sl}  TP: {tps}\n"
                    f"  Lot: {t.lot_size}  Tickets: {t.sub_tickets}"
                )
            else:
                lines.append(f"⬜ [{tm.channel_name}] No active trade")
        await event.reply("\n\n".join(lines) or "No channels monitored.")

    async def _on_positions(self, event: events.NewMessage.Event) -> None:
        if not self._mt5_client:
            await event.reply("MT5 client not available.")
            return
        try:
            positions = await self._mt5_client.get_open_positions_async()
            if not positions:
                await event.reply("No open MT5 positions.")
                return
            lines = []
            for p in positions:
                direction = "BUY" if p.type == 0 else "SELL"
                profit = p.profit
                sign = "+" if profit >= 0 else ""
                lines.append(
                    f"{p.symbol} {direction} {p.volume} @ {p.price_open}\n"
                    f"  SL:{p.sl}  TP:{p.tp}  P&L: {sign}{profit:.2f}"
                )
            await event.reply("\n".join(lines))
        except Exception as e:
            log.error("Bot /positions error: %s", e, exc_info=True)
            await event.reply(f"Error fetching positions: {e}")

    async def _on_stop(self, event: events.NewMessage.Event) -> None:
        if not self._mt5_client:
            await event.reply("MT5 client not available.")
            return
        try:
            positions = await self._mt5_client.get_open_positions_async()
            if not positions:
                await event.reply("No open positions to close.")
                return
            closed, failed = 0, 0
            for p in positions:
                result = await self._mt5_client.close_position_async(p.ticket)
                if result.success:
                    closed += 1
                else:
                    failed += 1
                    log.error("Bot /stop: failed to close ticket=%d: %s", p.ticket, result.error_message)
            await event.reply(f"Closed {closed} position(s). Failed: {failed}.")
        except Exception as e:
            log.error("Bot /stop error: %s", e, exc_info=True)
            await event.reply(f"Error: {e}")


"""Find the VIP channel ID by listing all channels/groups you're in."""

import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

async def main():
    client = TelegramClient("session", API_ID, API_HASH)
    await client.start()

    print("Looking for channels with 'NORTH' or 'VIP' or 'GOLD' in the name...\n")

    async for dialog in client.iter_dialogs():
        name = dialog.name or ""
        if any(kw in name.upper() for kw in ["NORTH", "VIP", "GOLD", "SIGNAL"]):
            print(f"  Name: {name}")
            print(f"  ID:   {dialog.id}")
            print(f"  Type: {'Channel' if dialog.is_channel else 'Group' if dialog.is_group else 'User'}")
            print()

    await client.disconnect()

asyncio.run(main())

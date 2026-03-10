import os, asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

api_id = int(os.getenv("TELEGRAM_API_ID"))
api_hash = os.getenv("TELEGRAM_API_HASH")

async def main():
    client = TelegramClient("session", api_id, api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (@{me.username}) | ID: {me.id}")

    channels = {
        -1002079334288: "TRUE NORTH - VIP",
        -1001417502545: "GOLD VIP Signal",
        -1001855862157: "TRADE WITH HASSNIN",
        -1002215693287: "TWM - XAUUSD ANALYSIS",
    }

    print("\nChannel access check:")
    for ch_id, name in channels.items():
        try:
            entity = await client.get_entity(ch_id)
            print(f"  OK  {name} ({ch_id})")
        except Exception as e:
            print(f"  FAIL  {name} ({ch_id}) — {e}")

    await client.disconnect()

asyncio.run(main())

"""Scrape historical messages from the TRUE NORTH - VIP channel."""

import asyncio
import csv
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

CHANNEL_ID = -1002079334288
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session")
OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_history.csv")
OUTPUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_history.json")


async def main():
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")

    if not api_id or not api_hash:
        print("Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env")
        sys.exit(1)

    client = TelegramClient(SESSION_PATH, api_id, api_hash)
    await client.start()

    print(f"Connected. Fetching messages from channel {CHANNEL_ID}...")

    messages = []
    count = 0

    async for msg in client.iter_messages(CHANNEL_ID, limit=None):
        count += 1
        text = msg.text or ""
        date_utc = msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else ""

        messages.append({
            "id": msg.id,
            "date_utc": date_utc,
            "text": text,
            "sender_id": msg.sender_id,
            "reply_to": msg.reply_to_msg_id if msg.reply_to_msg_id else None,
            "views": msg.views,
            "forwards": msg.forwards,
        })

        if count % 100 == 0:
            print(f"  Fetched {count} messages...")

    # Reverse to chronological order (oldest first)
    messages.reverse()

    print(f"\nTotal messages fetched: {len(messages)}")

    # Save as JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False)
    print(f"Saved JSON: {OUTPUT_JSON}")

    # Save as CSV
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "date_utc", "text", "sender_id", "reply_to", "views", "forwards"])
        writer.writeheader()
        writer.writerows(messages)
    print(f"Saved CSV: {OUTPUT_CSV}")

    # Print summary of signal-like messages
    print("\n--- Trading Signals Summary ---")
    signal_keywords = ["BUY", "SELL", "TP", "SL", "LIMIT", "CLOSE", "ENTRY", "BREAKEVEN"]
    signal_count = 0
    for msg in messages:
        text_upper = msg["text"].upper()
        if any(kw in text_upper for kw in signal_keywords):
            signal_count += 1
            preview = msg["text"].replace("\n", " | ")[:120]
            # Encode-safe for Windows console
            safe = preview.encode("ascii", errors="replace").decode("ascii")
            print(f"  [{msg['date_utc']}] {safe}")

    print(f"\nSignal-like messages: {signal_count} / {len(messages)}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

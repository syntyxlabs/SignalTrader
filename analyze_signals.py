"""Analyze TRUE NORTH VIP trading signals from scraped history."""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

INPUT = "C:/Projects/SignalTrader/channel_history.json"

def load_messages():
    with open(INPUT, encoding="utf-8") as f:
        return json.load(f)

def classify_message(text):
    """Simple rule-based classifier for signal messages."""
    upper = text.upper().replace("\n", " ")

    # New signal patterns
    if re.search(r'\b(BUY|SELL)\s*(NOW|LIMIT)?\b', upper) and re.search(r'\bTP\d?\b', upper):
        direction = "BUY" if "BUY" in upper else "SELL"
        execution = "LIMIT" if "LIMIT" in upper else "MARKET"
        return "NEW_SIGNAL", direction, execution

    # TP hit
    if re.search(r'\bTP\s*\d\s*(HIT|REACHED|DONE|!!!)', upper):
        return "TP_HIT", None, None

    # SL updates
    if re.search(r'\b(MOVE\s*SL|ADJUST\s*SL|SL\s*TO|EDIT\s*SL)', upper):
        return "SL_UPDATE", None, None

    # Close signals
    if re.search(r'\b(CLOSE|CLOSED|TRADE\s*CLOSED|BREAK\s*EVEN)', upper):
        return "CLOSE", None, None

    # SL hit
    if re.search(r'\bSL\s*(HIT|TRIGGERED)', upper):
        return "SL_HIT", None, None

    return "NOISE", None, None

def extract_signal_data(text):
    """Extract price, SL, TPs from a signal message."""
    upper = text.upper().replace("\n", " ")
    data = {}

    # Direction
    if "BUY" in upper:
        data["direction"] = "BUY"
    elif "SELL" in upper:
        data["direction"] = "SELL"

    # Execution type
    data["execution"] = "LIMIT" if "LIMIT" in upper else "MARKET"

    # Price - various formats
    price_match = re.search(r'PRICE[:\s]*(\d+(?:\.\d+)?)', upper)
    if price_match:
        data["price"] = float(price_match.group(1))

    # Price range (e.g., "PRICE 5025 - 5020")
    range_match = re.search(r'PRICE[:\s]*(\d+(?:\.\d+)?)\s*[-]\s*(\d+(?:\.\d+)?)', upper)
    if range_match:
        data["price_high"] = float(range_match.group(1))
        data["price_low"] = float(range_match.group(2))
        data["price"] = data["price_high"]

    # Entry price format
    entry_match = re.search(r'ENTRY\s*(?:AT|PRICE)?[:\s]*(\d+(?:\.\d+)?)', upper)
    if entry_match and "price" not in data:
        data["price"] = float(entry_match.group(1))

    # SL
    sl_match = re.search(r'SL[:\s]*(\d+(?:[.,]\d+)?)', upper)
    if sl_match:
        data["sl"] = float(sl_match.group(1).replace(",", "."))

    # TPs
    tps = []
    for tp_match in re.finditer(r'TP\s*\d?\s*[:\s]*(\d+(?:\.\d+)?)', upper):
        tps.append(float(tp_match.group(1)))
    if tps:
        data["tps"] = tps

    return data

def analyze():
    messages = load_messages()
    print(f"Total messages: {len(messages)}")
    print(f"Date range: {messages[0]['date_utc']} to {messages[-1]['date_utc']}")
    print()

    # Classify all messages
    signals = []
    tp_hits = []
    sl_updates = []
    closes = []
    sl_hits = []
    noise = []

    for msg in messages:
        text = msg["text"]
        if not text.strip():
            continue
        cls, direction, execution = classify_message(text)
        msg["_class"] = cls
        msg["_direction"] = direction
        msg["_execution"] = execution

        if cls == "NEW_SIGNAL":
            sig_data = extract_signal_data(text)
            msg["_data"] = sig_data
            signals.append(msg)
        elif cls == "TP_HIT":
            tp_hits.append(msg)
        elif cls == "SL_UPDATE":
            sl_updates.append(msg)
        elif cls == "CLOSE":
            closes.append(msg)
        elif cls == "SL_HIT":
            sl_hits.append(msg)
        else:
            noise.append(msg)

    print("=" * 60)
    print("MESSAGE CLASSIFICATION")
    print("=" * 60)
    print(f"  NEW_SIGNAL:  {len(signals)}")
    print(f"  TP_HIT:      {len(tp_hits)}")
    print(f"  SL_UPDATE:   {len(sl_updates)}")
    print(f"  CLOSE:       {len(closes)}")
    print(f"  SL_HIT:      {len(sl_hits)}")
    print(f"  NOISE:       {len(noise)}")
    print()

    # Direction breakdown
    buy_count = sum(1 for s in signals if s["_direction"] == "BUY")
    sell_count = sum(1 for s in signals if s["_direction"] == "SELL")
    print(f"  BUY signals:  {buy_count}")
    print(f"  SELL signals: {sell_count}")
    print()

    # Execution breakdown
    market_count = sum(1 for s in signals if s["_execution"] == "MARKET")
    limit_count = sum(1 for s in signals if s["_execution"] == "LIMIT")
    print(f"  MARKET orders: {market_count}")
    print(f"  LIMIT orders:  {limit_count}")
    print()

    # SL analysis
    print("=" * 60)
    print("STOP LOSS ANALYSIS")
    print("=" * 60)
    with_sl = [s for s in signals if "sl" in s.get("_data", {})]
    without_sl = [s for s in signals if "sl" not in s.get("_data", {})]
    print(f"  Signals with SL:    {len(with_sl)}")
    print(f"  Signals without SL: {len(without_sl)}")

    if with_sl:
        sl_distances = []
        for s in with_sl:
            d = s["_data"]
            if "price" in d and "sl" in d:
                dist = abs(d["price"] - d["sl"])
                sl_distances.append(dist)
        if sl_distances:
            print(f"  SL distance - min: ${min(sl_distances):.2f}")
            print(f"  SL distance - max: ${max(sl_distances):.2f}")
            print(f"  SL distance - avg: ${sum(sl_distances)/len(sl_distances):.2f}")
            print(f"  SL distance - median: ${sorted(sl_distances)[len(sl_distances)//2]:.2f}")
    print()

    # TP analysis
    print("=" * 60)
    print("TAKE PROFIT ANALYSIS")
    print("=" * 60)
    tp_counts = Counter()
    tp1_distances = []
    tp2_distances = []
    tp3_distances = []
    for s in signals:
        d = s.get("_data", {})
        tps = d.get("tps", [])
        tp_counts[len(tps)] += 1
        if "price" in d and tps:
            if len(tps) >= 1:
                tp1_distances.append(abs(tps[0] - d["price"]))
            if len(tps) >= 2:
                tp2_distances.append(abs(tps[1] - d["price"]))
            if len(tps) >= 3:
                tp3_distances.append(abs(tps[2] - d["price"]))

    print(f"  TP count distribution: {dict(sorted(tp_counts.items()))}")
    if tp1_distances:
        print(f"  TP1 distance - avg: ${sum(tp1_distances)/len(tp1_distances):.2f}")
    if tp2_distances:
        print(f"  TP2 distance - avg: ${sum(tp2_distances)/len(tp2_distances):.2f}")
    if tp3_distances:
        print(f"  TP3 distance - avg: ${sum(tp3_distances)/len(tp3_distances):.2f}")
    print()

    # Time of day analysis
    print("=" * 60)
    print("TIMING ANALYSIS")
    print("=" * 60)
    hour_counts = Counter()
    day_counts = Counter()
    for s in signals:
        dt = datetime.strptime(s["date_utc"], "%Y-%m-%d %H:%M:%S")
        hour_counts[dt.hour] += 1
        day_counts[dt.strftime("%A")] += 1

    print("  Signals by hour (UTC):")
    for h in sorted(hour_counts.keys()):
        bar = "#" * hour_counts[h]
        print(f"    {h:02d}:00  {hour_counts[h]:3d}  {bar}")

    print()
    print("  Signals by day of week:")
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for d in day_order:
        if d in day_counts:
            bar = "#" * day_counts[d]
            print(f"    {d:10s}  {day_counts[d]:3d}  {bar}")
    print()

    # Win/Loss tracking (attempt to pair signals with outcomes)
    print("=" * 60)
    print("TRADE OUTCOMES (estimated)")
    print("=" * 60)

    # Build timeline of events
    tp_hit_count = len(tp_hits)
    sl_hit_count = len(sl_hits)
    close_count = len(closes)
    breakeven = sum(1 for c in closes if "BREAK EVEN" in c["text"].upper() or "BREAKEVEN" in c["text"].upper())

    print(f"  Total TP hits:      {tp_hit_count}")
    print(f"  Total SL hits:      {sl_hit_count}")
    print(f"  Total closes:       {close_count}")
    print(f"  Breakeven closes:   {breakeven}")
    print()

    # Monthly breakdown
    print("=" * 60)
    print("MONTHLY SIGNAL COUNT")
    print("=" * 60)
    monthly = Counter()
    for s in signals:
        month = s["date_utc"][:7]  # YYYY-MM
        monthly[month] += 1

    for m in sorted(monthly.keys()):
        bar = "#" * monthly[m]
        print(f"  {m}  {monthly[m]:3d}  {bar}")
    print()

    # Price range analysis
    print("=" * 60)
    print("PRICE RANGE ENTRIES")
    print("=" * 60)
    range_signals = [s for s in signals if "price_high" in s.get("_data", {})]
    single_price = [s for s in signals if "price" in s.get("_data", {}) and "price_high" not in s.get("_data", {})]
    print(f"  Price range entries (e.g., 5025-5020): {len(range_signals)}")
    print(f"  Single price entries: {len(single_price)}")
    print()

    # Print all signals with data for manual review
    print("=" * 60)
    print("ALL TRADING SIGNALS (chronological)")
    print("=" * 60)
    for i, s in enumerate(signals, 1):
        d = s.get("_data", {})
        text_preview = s["text"].replace("\n", " | ")[:100]
        safe = text_preview.encode("ascii", errors="replace").decode("ascii")
        line = f"  #{i:3d} [{s['date_utc']}] {s['_direction']:4s} {s['_execution']:6s}"
        if "price" in d:
            line += f" @ {d['price']:.2f}"
        if "sl" in d:
            line += f" SL:{d['sl']:.2f}"
        if "tps" in d:
            line += f" TPs:{d['tps']}"
        print(line)
        print(f"        {safe}")
        print()


if __name__ == "__main__":
    analyze()

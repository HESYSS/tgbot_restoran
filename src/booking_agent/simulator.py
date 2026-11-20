from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .engine import BookingEngine
from .models import BookingRequest, ConversationState
from .providers import RuleBasedProvider
from .store import StateStore


async def main() -> None:
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--restaurant", required=True)
    args = arguments.parse_args()
    request = BookingRequest.for_today(args.restaurant, datetime.now(ZoneInfo("Europe/London")).date())
    store = StateStore(Path("data/simulated-state.json"), Path("data/simulated-decisions.jsonl"))
    engine = BookingEngine(ConversationState(request), RuleBasedProvider(), store)
    print("AGENT:", await engine.start())
    manager = f"Certainly, your table for two is confirmed for {request.booking_date} at 17:00."
    print("MANAGER:", manager)
    print("AGENT:", await engine.receive(manager))
    print("STATUS:", engine.state.status)

if __name__ == "__main__":
    asyncio.run(main())

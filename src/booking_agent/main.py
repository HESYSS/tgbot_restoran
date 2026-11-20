from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .engine import BookingEngine
from .models import BookingRequest, ConversationState
from .providers import configured_provider
from .store import StateStore
from .telegram_client import run_telegram


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--manager", required=True)
    result.add_argument("--live", action="store_true", help="Allow sending messages (only after testing)")
    result.add_argument("--dry-run", action="store_true", help="Connect but never send messages")
    result.add_argument("--confirm-send", action="store_true", help="Required with --live as an explicit acknowledgement")
    result.add_argument("--notify-self", action="store_true", help="Send status alerts only to this account's Saved Messages")
    return result


async def main() -> None:
    args = parser().parse_args()
    if args.live == args.dry_run:
        raise SystemExit("Choose exactly one of --live or --dry-run.")
    if args.live and not args.confirm_send:
        raise SystemExit("Live sending requires --confirm-send. Use --dry-run first.")
    load_dotenv()
    store = StateStore(Path("data/state.json"), Path("data/decisions.jsonl"))
    requested = BookingRequest.for_today("Unknown restaurant", datetime.now(ZoneInfo("Europe/London")).date())
    state = store.load()
    if state and state.request != requested:
        raise SystemExit("Saved state does not match this restaurant/date. Archive data/state.json before starting a new booking.")
    state = state or ConversationState(requested)
    engine = BookingEngine(state, configured_provider(), store)
    await run_telegram(engine, args.manager, "client-default", args.dry_run, args.notify_self)

if __name__ == "__main__":
    asyncio.run(main())

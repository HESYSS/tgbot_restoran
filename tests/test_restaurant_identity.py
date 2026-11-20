import asyncio
from datetime import date

from booking_agent.engine import BookingEngine
from booking_agent.models import BookingRequest, ConversationState, Status
from booking_agent.providers import RuleBasedProvider
from booking_agent.store import StateStore


def test_manager_provided_restaurant_name_is_used(tmp_path):
    request = BookingRequest.for_today("", date(2026, 11, 19))
    engine = BookingEngine(ConversationState(request), RuleBasedProvider(), StateStore(tmp_path / "state.json", tmp_path / "log.jsonl"))
    asyncio.run(engine.start())
    reply = asyncio.run(engine.receive("This is Example Bistro. Your table for two is confirmed for 2026-11-19 at 17:00."))
    assert engine.state.status is Status.CONFIRMED
    assert "Example Bistro" in reply

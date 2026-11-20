import asyncio
from datetime import date

import pytest

from booking_agent.engine import BookingEngine
from booking_agent.models import BookingRequest, ConversationState, Status
from booking_agent.providers import RuleBasedProvider
from booking_agent.store import StateStore


@pytest.fixture
def engine(tmp_path):
    request = BookingRequest.for_today("Test Bistro", date(2026, 11, 19))
    return BookingEngine(ConversationState(request), RuleBasedProvider(), StateStore(tmp_path / "state.json", tmp_path / "log.jsonl"))


def test_explicit_confirmation_is_repeated(engine):
    asyncio.run(engine.start())
    reply = asyncio.run(engine.receive("Your table for two is confirmed for 2026-11-19 at 17:00."))
    assert engine.state.status is Status.CONFIRMED
    assert "Test Bistro" in reply and "2 people" in reply


def test_ambiguous_confirmation_needs_clarification(engine):
    asyncio.run(engine.start())
    reply = asyncio.run(engine.receive("Confirmed, see you then."))
    assert engine.state.status is Status.NEEDS_CLARIFICATION
    assert "confirm" in reply.lower()


def test_payment_request_is_escalated(engine):
    asyncio.run(engine.start())
    reply = asyncio.run(engine.receive("Please use this payment link to pay a deposit."))
    assert reply is None
    assert engine.state.status is Status.ESCALATED


def test_blank_restaurant_is_rejected():
    assert BookingRequest.for_today("  ", date.today()).restaurant_name == "Unknown restaurant"

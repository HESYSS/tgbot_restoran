from booking_agent.models import ConversationState, Status
from booking_agent.engine import BookingEngine
from booking_agent.store import StateStore
from booking_agent.restaurant import REMINDER, introduced_name
from test_llm_policy import build, decision, run


def test_intro_remembered_even_when_model_returns_no_name(tmp_path):
    engine = build(tmp_path, decision('unavailable', restaurant=None))
    run(engine.receive('Good afternoon, this is Test Bistro. Unfortunately, we do not have a table at 6:30 pm.'))
    assert engine.state.request.restaurant_name == 'Test Bistro'
    engine.provider.decision = decision('offer_time', restaurant=None, time='19:00')
    reply = run(engine.receive('We can offer a table for four at 7 pm.'))
    assert 'restaurant name' not in reply
    engine.provider.decision = decision('confirm', restaurant=None, time='19:00')
    reply = run(engine.receive('Confirmed for four guests on 19 November 2026 at 7 pm.'))
    assert engine.state.status == Status.CONFIRMED
    assert 'Test Bistro' in reply


def test_name_asked_only_when_other_details_confirmed(tmp_path):
    engine = build(tmp_path, decision('offer_time', restaurant=None, time='19:00'))
    reply = run(engine.receive('We can offer a table for four at 7 pm.'))
    assert 'restaurant name' not in reply
    engine.provider.decision = decision('confirm', restaurant=None, time='19:00')
    reply = run(engine.receive('Confirmed for four guests on 19 November 2026 at 7 pm.'))
    assert reply == REMINDER
    assert engine.state.status != Status.CONFIRMED
    assert engine.state.pending_confirmation
    assert not engine.state.pending_question  # Ask the restaurant, not the client.


def test_name_completes_confirmation_after_reload(tmp_path):
    engine = build(tmp_path, decision('confirm', restaurant=None))
    run(engine.receive('Confirmed for four guests on 19 November 2026 at 6:30 pm.'))
    store = StateStore(tmp_path/'state.json', tmp_path/'log.jsonl')
    restored = BookingEngine(store.load(), engine.provider, store)
    restored.provider.decision = decision('reply', message='Thank you.', restaurant=None)
    reply = run(restored.receive('This is Test Bistro.'))
    assert restored.state.status == Status.CONFIRMED
    assert 'Test Bistro' in reply and '6:30 pm' in reply


def test_new_contradiction_clears_old_confirmation(tmp_path):
    engine = build(tmp_path, decision('confirm', restaurant=None))
    run(engine.receive('Confirmed for four guests on 19 November 2026 at 6:30 pm.'))
    engine.provider.decision = decision('declined', restaurant=None)
    run(engine.receive('This is Test Bistro. Sorry, we are fully booked for the entire evening.'))
    assert engine.state.status == Status.CANCELLED
    assert engine.state.pending_confirmation is None


def test_intro_stops_at_comma():
    assert introduced_name('This is Test Bistro, how can I help?') == 'Test Bistro'
    assert introduced_name('This is confirmed for tomorrow.') is None

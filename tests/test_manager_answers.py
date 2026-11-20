from datetime import date
import pytest
from booking_agent.engine import BookingEngine
from booking_agent.models import BookingRequest, ConversationState, Status
from booking_agent.providers import RuleBasedProvider
from booking_agent.store import StateStore


def immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError('Unexpected network or asynchronous I/O')


@pytest.fixture
def engine(tmp_path):
    request = BookingRequest.for_today('Test Bistro', date(2026, 11, 19))
    request.booking_name = 'Test Client'
    return BookingEngine(ConversationState(request, status=Status.AWAITING_REPLY), RuleBasedProvider(), StateStore(tmp_path/'state.json', tmp_path/'log.jsonl'))


@pytest.mark.parametrize('question, expected', [
    ('What name should I book under?', 'Test Client'),
    ('How many guests?', '2 people'),
    ('What time?', '17:00'),
    ('Which day?', '2026-11-19'),
])
def test_known_questions(engine, question, expected):
    assert expected in immediate(engine.receive(question))


def test_unknown_question_waits_for_client(engine):
    assert immediate(engine.receive('Do you have any allergies?')) is None
    assert engine.state.pending_question
    assert engine.answer_pending_question('No allergies.') == 'No allergies.'
    assert engine.state.pending_question is None


def test_outside_time_requires_client(engine):
    assert immediate(engine.receive('Could you come at 19:00?')) is None
    assert engine.state.request.booking_time == '17:00'


def test_allowed_time_needs_confirmation(engine):
    engine.state.request.earliest = '16:15'
    engine.state.request.latest = '17:45'
    result = immediate(engine.receive('Could you come at 17:30?'))
    assert '17:30' in result
    assert engine.state.status != Status.CONFIRMED


def test_negative_confirmation(engine):
    immediate(engine.receive('Not confirmed for two on 2026-11-19 at 17:00'))
    assert engine.state.status != Status.CONFIRMED

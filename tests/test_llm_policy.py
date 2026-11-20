import httpx
import pytest
from booking_agent.engine import BookingEngine
from booking_agent.models import BookingRequest, ConversationState, Status
from booking_agent.store import StateStore


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError('Unexpected network call')


class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.context = None

    async def decide(self, request, history):
        self.context = (request, list(history))
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


def decision(action='confirm', **changes):
    value = dict(action=action, message='Please advise.', reason='Test decision',
                 restaurant='Test Bistro', date='2026-11-19', time='18:30', guests=4)
    return value | changes


def build(tmp_path, value):
    request = BookingRequest('Unknown restaurant', '2026-11-19', booking_time='18:30', party_size=4,
                             booking_name='Test Client', earliest='18:00', latest='19:00')
    return BookingEngine(ConversationState(request), FakeLLM(value), StateStore(tmp_path/'state.json', tmp_path/'log.jsonl'))


def test_confirmation_uses_request_and_context(tmp_path):
    engine = build(tmp_path, decision())
    reply = run(engine.receive('Test Bistro: confirmed for 4 guests on 2026-11-19 at 18:30.'))
    assert engine.state.status == Status.CONFIRMED
    assert 'for four' in reply and '6:30 pm' in reply
    assert engine.provider.context[0].booking_name == 'Test Client'


def test_natural_confirmation_from_actual_test(tmp_path):
    engine = build(tmp_path, decision())
    reply = run(engine.receive('Your table for four at Test Bistro is confirmed for 19 November 2026 at 6:30 pm.'))
    assert engine.state.status == Status.CONFIRMED
    assert '19 November 2026' in reply and '6:30 pm' in reply


@pytest.mark.parametrize('text', [
    'Test Bistro: not yet confirmed for 4 guests on 2026-11-19 at 18:30.',
    'Test Bistro: confirmed for 3 guests on 2026-11-19 at 18:30.',
    'Test Bistro: confirmed for 4 guests on 2026-11-20 at 18:30.',
    'Test Bistro: confirmed for 4 guests on 2026-11-19 at 20:30.',
])
def test_hallucinated_confirmation_rejected(tmp_path, text):
    engine = build(tmp_path, decision())
    run(engine.receive(text))
    assert engine.state.status != Status.CONFIRMED


def test_unknown_question_and_client_context(tmp_path):
    engine = build(tmp_path, decision('ask_client', message='Есть ли аллергии?'))
    assert run(engine.receive('Any allergies?')) is None
    assert engine.state.pending_question == 'Any allergies?'
    engine.answer_pending_question('No allergies.')
    assert {'role': 'client', 'content': 'No allergies.'} in engine.state.messages


def test_rate_limit_pauses_dialogue(tmp_path):
    engine = build(tmp_path, httpx.RequestError('connection unavailable'))
    assert run(engine.receive('Hello')) is None
    assert engine.state.status == Status.ESCALATED
    assert engine.state.pending_question is None


def test_reply_cannot_bypass_confirmation(tmp_path):
    engine = build(tmp_path, decision('reply', message='Your table is confirmed.'))
    assert run(engine.receive('Maybe available.')) is None
    assert engine.state.status != Status.CONFIRMED

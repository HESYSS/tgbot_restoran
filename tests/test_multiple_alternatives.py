import pytest
from booking_agent.offered_times import alternatives
from booking_agent.models import Status
from test_llm_policy import build, decision, run


@pytest.mark.parametrize('message', [
    "Good morning. I'm afraid 5 is fully booked tonight. I could do half five or six. And could I take a contact number, please?",
    'Half five or six.',
    'I can offer 17:30 or 18:00 today.',
])
def test_extract_all_offered_times_and_exclude_rejected_five(message):
    assert alternatives(message, '17:00') == {'17:30', '18:00'}


@pytest.mark.parametrize('action', ['offer_time', 'unavailable', 'ask_client'])
def test_exact_time_declines_all_alternatives_even_if_model_misclassifies(tmp_path, action):
    engine = build(tmp_path, decision(action, time='17:30', client_question='Какой контактный номер?'))
    r = engine.state.request
    r.booking_time = r.earliest = r.latest = '17:00'
    reply = run(engine.receive("I'm afraid 5 is fully booked tonight. I could do half five or six. And could I take a contact number, please?"))
    assert engine.state.status == Status.CANCELLED
    assert 'would not work' in reply
    assert not engine.state.pending_question
    assert r.booking_time == '17:00'


def test_choose_in_range_option_not_first_model_choice(tmp_path):
    engine = build(tmp_path, decision('offer_time', time='18:00'))
    r = engine.state.request
    r.booking_time, r.earliest, r.latest = '17:00', '16:30', '17:30'
    reply = run(engine.receive('I can offer 17:30 or 18:00 today.'))
    assert r.booking_time == '17:30'
    assert '5:30 pm' in reply
    assert engine.state.status == Status.AWAITING_REPLY


def test_contact_question_not_lost_with_in_range_offer(tmp_path):
    engine = build(tmp_path, decision('offer_time', time='17:30', client_question='Какой контактный номер можно передать?'))
    r = engine.state.request
    r.booking_time, r.earliest, r.latest = '17:00', '16:30', '17:30'
    reply = run(engine.receive('I could do half five or six. Could I take a contact number, please?'))
    assert reply is None
    assert r.booking_time == '17:30'
    assert engine.state.pending_question
    assert 'контактный номер' in engine.state.escalation_reason
    outgoing = engine.answer_pending_question('Use the contact number I provided.')
    assert outgoing.startswith('5:30 pm would be lovely')
    assert outgoing.endswith('Use the contact number I provided.')


def test_party_size_and_phone_digits_not_treated_as_time():
    assert alternatives('We can offer a table for two at 17:30. Contact number please?', '17:00') == {'17:30'}
    assert alternatives('We can offer a table for two.', '17:00') == set()


def test_unparseable_offer_requests_client_review_instead_of_loop(tmp_path):
    engine = build(tmp_path, decision('offer_time', time='17:30'))
    first = run(engine.receive('We could offer a different slot.'))
    assert '24-hour format' in first
    assert run(engine.receive('We could offer a different slot.')) is None
    assert engine.state.pending_question
    assert 'по кругу' in engine.state.escalation_reason


@pytest.mark.parametrize('action', ['ask_client', 'offer_time', 'reply'])
def test_preference_question_is_answered_without_client(tmp_path, action):
    engine = build(tmp_path, decision(action, time='17:30', message='Какое время вы предпочитаете?', client_question='Какое время вы предпочитаете?'))
    r = engine.state.request
    r.booking_time, r.earliest, r.latest = '17:30', '16:30', '17:30'
    reply = run(engine.receive('Thank you. Would you prefer half five or six?'))
    assert reply.startswith('5:30 pm would be lovely')
    assert not engine.state.pending_question
    assert engine.state.pending_reply_prefix is None


def test_reply_prefix_persists_after_reload(tmp_path):
    from booking_agent.engine import BookingEngine
    from booking_agent.store import StateStore
    engine = build(tmp_path, decision('offer_time', time='17:30', client_question='Какой контактный номер?'))
    r = engine.state.request
    r.booking_time, r.earliest, r.latest = '17:00', '16:30', '17:30'
    run(engine.receive('I could do half five or six. Could I take a contact number, please?'))
    store = StateStore(tmp_path/'state.json', tmp_path/'log.jsonl')
    restored = BookingEngine(store.load(), engine.provider, store)
    outgoing = restored.answer_pending_question('My contact is the test number provided.')
    assert outgoing == '5:30 pm would be lovely, thank you. My contact is the test number provided.'
    assert restored.state.pending_reply_prefix is None
    assert restored.state.messages[-2] == {'role': 'client', 'content': 'My contact is the test number provided.'}


def test_separate_question_after_preference_is_preserved(tmp_path):
    engine = build(tmp_path, decision('offer_time', time='17:30', client_question='Есть ли аллергии?'))
    r = engine.state.request
    r.booking_time, r.earliest, r.latest = '17:00', '16:30', '17:30'
    assert run(engine.receive('Would you prefer half five or six? Does anyone have allergies?')) is None
    assert 'аллергии' in engine.state.escalation_reason
    assert '5:30 pm' in engine.state.pending_reply_prefix

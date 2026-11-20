import pytest
from booking_agent.models import ConversationState, Status
from booking_agent.engine import BookingEngine
from booking_agent.booking_form import ASK, advance, prompt
from datetime import date
from test_llm_policy import build, decision, run


def pending(tmp_path, message='I can offer 17:30 or 18:00 today.'):
    engine = build(tmp_path, decision('offer_time', time='17:30'))
    r = engine.state.request
    r.booking_time = r.earliest = r.latest = '17:00'
    r.ask_before_alternative = True
    assert run(engine.receive(message)) is None
    return engine


def test_new_option_is_explicit_and_keeps_initial_time():
    form = dict(step='flexibility', booking_time='17:00')
    assert ASK in prompt(form)[1]
    advance(form, ASK, date(2030, 1, 15))
    assert form['ask_before_alternative']
    assert form['earliest'] == form['latest'] == '17:00'
    assert form['step'] == 'restaurant'


def test_offer_waits_for_owner_without_external_reply(tmp_path):
    engine = pending(tmp_path, "Five is fully booked. I could do half five or six.")
    assert engine.state.status == Status.NEEDS_CLARIFICATION
    assert engine.state.pending_time_offer['times'] == ['17:30', '18:00']
    assert engine.state.request.booking_time == '17:00'
    assert not any(row['direction'] == 'outgoing' for row in engine.store.messages())


@pytest.mark.parametrize('answer', ['да', 'yes', '17:45', '17:30 or 18:00'])
def test_no_guess_or_unoffered_time_is_sent(tmp_path, answer):
    engine = pending(tmp_path)
    with pytest.raises(ValueError):
        engine.answer_pending_question(answer)
    assert engine.state.pending_time_offer
    assert engine.state.request.booking_time == '17:00'


def test_owner_can_select_then_manager_must_confirm(tmp_path):
    engine = pending(tmp_path)
    reply = engine.answer_pending_question('17:30')
    assert '5:30 pm would be lovely' in reply
    assert engine.state.request.earliest == engine.state.request.latest == '17:30'
    assert engine.state.status == Status.AWAITING_REPLY
    engine.provider.decision = decision('confirm', time='17:30', guests=4)
    final = run(engine.receive('Your table for four people at Test Bistro is confirmed for 19 November 2026 at 17:30.'))
    assert engine.state.status == Status.CONFIRMED
    assert '5:30 pm' in final


def test_owner_can_refuse(tmp_path):
    engine = pending(tmp_path)
    assert 'would not work' in engine.answer_pending_question('Нет')
    assert engine.state.status == Status.CANCELLED


def test_waiting_offer_survives_reload(tmp_path):
    engine = pending(tmp_path)
    restored = BookingEngine(engine.store.load(), engine.provider, engine.store)
    assert restored.state.request.ask_before_alternative
    assert restored.state.pending_time_offer['times'] == ['17:30', '18:00']
    assert '6 pm would be lovely' in restored.answer_pending_question('18:00')


def test_new_manager_message_invalidates_old_selection(tmp_path):
    engine = pending(tmp_path)
    engine.provider.decision = decision('ask_client', message='Нужен номер телефона.')
    run(engine.receive('Could I take a contact number?'))
    assert engine.state.pending_time_offer is None
    assert engine.state.request.booking_time == '17:00'


def test_contact_request_remains_after_owner_selects_time(tmp_path):
    engine = pending(tmp_path, 'I could do half five or six. Could I take a contact number?')
    assert '5:30 pm' in engine.answer_pending_question('17:30')
    assert engine.state.pending_question
    assert 'номер' in engine.state.escalation_reason
    assert engine.answer_pending_question('Use my supplied contact number.') == 'Use my supplied contact number.'


def test_old_state_defaults_to_strict_policy(tmp_path):
    engine = pending(tmp_path)
    state = engine.state.to_dict()
    state['request'].pop('ask_before_alternative')
    state.pop('pending_time_offer')
    old = ConversationState.from_dict(state)
    assert not old.request.ask_before_alternative
    assert old.pending_time_offer is None

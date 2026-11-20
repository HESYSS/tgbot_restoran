from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_llm_policy import build, decision, run
from booking_agent.models import Status
from booking_agent.commercial_control_bot import CommercialControlPanel, NEW_BOOKING, ANSWER_MANAGER, STOP_DIALOGUE


@pytest.mark.parametrize('incoming', ['Unfortunately, we don’t have a table available at 6:30 pm.', 'We are fully booked at 6:30 pm.'])
def test_refusal_without_alternative_never_reasks_client(tmp_path, incoming):
    engine = build(tmp_path, decision('ask_client', message='Какое время вам подходит?'))
    reply = run(engine.receive(incoming))
    assert 'What times do you have available?' in reply
    assert not engine.state.pending_question
    assert engine.state.status == Status.AWAITING_REPLY


@pytest.mark.parametrize('proposed', ['18:00', '19:00'])
def test_boundaries_accepted_as_guest(tmp_path, proposed):
    engine = build(tmp_path, decision('offer_time', time=proposed, date=None, guests=4))
    reply = run(engine.receive(f'We can offer a table for four at {proposed}.'))
    assert 'would be lovely' in reply
    assert 'We can offer' not in reply
    assert engine.state.request.booking_time == proposed
    assert engine.state.status != Status.CONFIRMED
    assert not engine.state.pending_question


def test_wrong_model_role_cannot_repeat_restaurant_offer(tmp_path):
    engine = build(tmp_path, decision('reply', time=None, message='We can offer a table for four at 7 pm. Would you like to confirm?'))
    reply = run(engine.receive('We can offer a table for four at 7 pm.'))
    assert 'would be lovely' in reply
    assert 'We can offer' not in reply


def test_outside_window_declined_without_client_question(tmp_path):
    engine = build(tmp_path, decision('offer_time', time='20:00'))
    reply = run(engine.receive('We can offer a table for four at 8 pm.'))
    assert 'would not work' in reply
    assert engine.state.status == Status.CANCELLED
    assert not engine.state.pending_question


@pytest.mark.parametrize('action', ['declined', 'unavailable', 'ask_client'])
def test_final_refusal_returns_farewell(tmp_path, action):
    engine = build(tmp_path, decision(action))
    reply = run(engine.receive('We are fully booked for the rest of the evening.'))
    assert 'Thank you' in reply and 'lovely evening' in reply
    assert engine.state.status == Status.CANCELLED


def test_availability_with_unknown_allergies_is_still_client_question(tmp_path):
    engine = build(tmp_path, decision('ask_client', message='Есть ли аллергии?'))
    assert run(engine.receive('We have a table at 6:30 pm. Does anyone have allergies?')) is None
    assert engine.state.pending_question


def test_finish_notification_restores_main_menu():
    panel = object.__new__(CommercialControlPanel)
    panel.local_dialogues = {101: SimpleNamespace(stopped=True)}
    panel.answering_manager = {101}
    panel.registry = SimpleNamespace(remove_route=lambda chat_id: None)
    panel.bot = SimpleNamespace(send_message=AsyncMock())
    run(panel.notify_dialogue(101, 'Диалог завершён'))
    keyboard = panel.bot.send_message.call_args.kwargs['reply_markup']
    labels = {b.text for row in keyboard.keyboard for b in row}
    assert NEW_BOOKING in labels
    assert ANSWER_MANAGER not in labels and STOP_DIALOGUE not in labels
    assert 101 not in panel.answering_manager

from datetime import date
import pytest
from booking_agent.booking_form import advance, EXACT, FLEX, BEFORE, RANGE

TODAY = date(2026, 11, 19)


def test_complete_booking_and_exact_time():
    form = {"step": "name"}
    for value in ["Test Client", "2", "Сегодня", "17:00", EXACT, "Не знаю", "@project_sync_bot"]:
        advance(form, value, TODAY)
    assert form['step'] == 'review'
    assert form['earliest'] == form['latest'] == '17:00'
    assert form['restaurant_name'] == 'Unknown restaurant'


def test_flexible_window():
    form = {'step': 'flexibility', 'booking_time': '17:00'}
    advance(form, FLEX, TODAY)
    assert form['step'] == 'tolerance'
    advance(form, '45', TODAY)
    assert (form['earliest'], form['latest']) == ('16:15', '17:45')


def test_deadline_requires_lower_bound():
    form = {'step': 'flexibility', 'booking_time': '17:00'}
    advance(form, BEFORE, TODAY)
    with pytest.raises(ValueError):
        advance(form, '18:00', TODAY)
    advance(form, '16:00', TODAY)
    assert form['earliest'] == '16:00' and form['latest'] == '17:00'


@pytest.mark.parametrize('value', ['25:00', '17:70', '5pm'])
def test_reject_bad_time(value):
    form = {'step': 'time'}
    with pytest.raises(ValueError):
        advance(form, value, TODAY)
    assert form == {'step': 'time'}


def test_range_and_past_date():
    with pytest.raises(ValueError):
        advance({'step': 'date'}, '2026-11-18', TODAY)
    form = {'step': 'flexibility', 'booking_time': '17:00'}
    advance(form, RANGE, TODAY)
    advance(form, '16:00-18:00', TODAY)
    assert form['step'] == 'restaurant'


@pytest.mark.parametrize('value', ['16:00-22:59', 'от 16:00 до 22:59', '16:00 — 22:59'])
def test_natural_range(value):
    form = {'step': 'range', 'booking_time': '17:00'}
    advance(form, value, TODAY)
    assert (form['earliest'], form['latest']) == ('16:00', '22:59')


@pytest.mark.parametrize('value', ['0', '-2', '2.5', 'два', '31', '²', '9' * 5000])
def test_invalid_guests(value):
    form = {'step': 'guests'}
    with pytest.raises(ValueError):
        advance(form, value, TODAY)
    assert form['step'] == 'guests'


def test_range_entered_in_time_step_explains_next_step():
    with pytest.raises(ValueError, match='следующем шаге'):
        advance({'step': 'time'}, '16:00-22:59', TODAY)


@pytest.mark.parametrize('recipient', ['@project_sync_bot', '@vlad_dev_7'])
def test_recipient_is_only_set_from_user_input(recipient):
    form = {'step': 'manager'}
    advance(form, recipient, TODAY)
    assert form['manager'] == recipient
    assert form['recipient_entered_manually'] is True


def test_recipient_has_no_suggested_button():
    from booking_agent.booking_form import prompt
    assert prompt({'step': 'manager'})[1] == []

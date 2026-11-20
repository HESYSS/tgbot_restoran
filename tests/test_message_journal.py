import json
import pytest
from booking_agent.store import StateStore
from booking_agent.local_dialogue import LocalDialogue


def test_one_row_per_message_and_delivery(tmp_path):
    store = StateStore(tmp_path/'state.json', tmp_path/'decisions.jsonl')
    incoming = store.log('incoming', 'What name?', 'Manager reply received', 'Classifying')
    store.log('decision', '', 'Manager asks booking name', 'Answer from client request')
    outgoing = store.log('outgoing', 'Test Client\nThank you.', 'Known name', 'Reply with verified name')
    assert len(store.messages()) == 2
    assert store.messages()[0]['understood'] == 'Manager asks booking name'
    assert store.messages()[1]['delivery'] == 'prepared'
    with pytest.raises(ValueError):
        store.delivery(outgoing, 'sent')
    store.delivery(outgoing, 'sent', 123)
    reloaded = StateStore(tmp_path/'state.json', tmp_path/'decisions.jsonl')
    rows = reloaded.messages()
    assert len(rows) == 2 and rows[1]['telegram_message_id'] == 123
    assert rows[0]['id'] == incoming
    exported = reloaded.export_text().read_text(encoding='utf-8').splitlines()
    assert len(exported) == 2
    assert 'Понял:' in exported[0] and 'Решил:' in exported[0]
    assert '\\n' in exported[1]


def test_repeated_same_text_is_not_lost(tmp_path):
    store = StateStore(tmp_path/'state.json', tmp_path/'decisions.jsonl')
    first = store.log('incoming', 'Hello', 'Greeting', 'Wait')
    second = store.log('incoming', 'Hello', 'Repeated greeting', 'Wait')
    assert first != second and len(store.messages()) == 2


def test_local_bookings_have_separate_journals(tmp_path):
    form = dict(booking_date='2030-01-15', booking_time='17:00', booking_name='Test Client',
                party_size=2, earliest='17:00', latest='17:00')
    a, b = LocalDialogue(form, tmp_path), LocalDialogue(form, tmp_path)
    assert a.engine.store.journal_path != b.engine.store.journal_path
    operation = a.next_question()
    with pytest.raises(StopIteration):
        operation.send(None)
    rows = a.engine.store.messages()
    assert len(rows) == 2 and rows[1]['delivery'] == 'simulated'
    assert not b.engine.store.messages()

from booking_agent.local_dialogue import LocalDialogue
from booking_agent.models import Status


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError('Local test unexpectedly used asynchronous I/O')


def test_complete_manager_client_roundtrip(tmp_path):
    dialogue = LocalDialogue(dict(booking_date='2026-11-19', booking_time='17:00',
        booking_name='Test Client', party_size=2, earliest='17:00', latest='17:00'), tmp_path)
    for expected in ['Test Client', '2 people', '2026-11-19', '17:00']:
        question, reply = run(dialogue.next_question())
        assert expected in reply
    _, reply = run(dialogue.next_question())
    assert reply is None
    assert dialogue.engine.state.pending_question
    assert dialogue.answer('No allergies.') == 'No allergies.'
    _, reply = run(dialogue.next_question())
    assert 'Test Bistro' in reply
    assert dialogue.engine.state.status == Status.CONFIRMED
    assert dialogue.stopped

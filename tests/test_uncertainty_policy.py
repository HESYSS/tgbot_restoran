import httpx
from booking_agent.models import Status
from test_llm_policy import build, decision, run


def test_restaurant_uncertainty_goes_to_manager(tmp_path):
    engine = build(tmp_path, decision('ask_manager', message='Could you clarify whether you mean today or tomorrow?'))
    reply = run(engine.receive('It might be tomorrow, let me check.'))
    assert 'today or tomorrow' in reply
    assert engine.state.pending_question is None
    assert engine.state.status == Status.AWAITING_REPLY


def test_personal_unknown_goes_to_client(tmp_path):
    engine = build(tmp_path, decision('ask_client', message='Есть ли у гостей аллергии?'))
    assert run(engine.receive('Does anyone have allergies?')) is None
    assert engine.state.pending_question
    assert engine.state.status == Status.NEEDS_CLARIFICATION


def test_api_error_does_not_forward_stale_question(tmp_path):
    engine = build(tmp_path, httpx.RequestError('unavailable'))
    engine.state.pending_question = 'Old personal question'
    engine.state.pending_reply_prefix = 'Stale time acceptance'
    assert run(engine.receive('Could you confirm?')) is None
    assert engine.state.status == Status.ESCALATED
    assert engine.state.pending_question is None
    assert engine.state.pending_reply_prefix is None
    assert not any(row['direction'] == 'outgoing' for row in engine.store.messages())

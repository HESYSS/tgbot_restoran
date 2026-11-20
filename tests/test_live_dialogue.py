from types import SimpleNamespace
import pytest

from booking_agent.live_dialogue import LiveDialogue
from booking_agent.providers import RuleBasedProvider


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError('Unexpected real I/O in mock Telegram test')


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.handlers = []
        self.fail = False

    def is_connected(self):
        return True

    async def get_entity(self, handle):
        return SimpleNamespace(id=77, username=handle.lstrip('@'))

    async def get_me(self):
        return SimpleNamespace(id=22)

    def add_event_handler(self, handler, event):
        self.handlers.append(handler)

    def remove_event_handler(self, handler):
        self.handlers.remove(handler)

    async def send_message(self, target, text, **kwargs):
        if self.fail:
            raise OSError('mock disconnect')
        self.sent.append((target.id, text))
        return SimpleNamespace(id=len(self.sent))


@pytest.fixture
def setup(tmp_path):
    client = FakeTelegram()
    notifications = []
    async def notify(text):
        notifications.append(text)
    form = dict(manager='@project_sync_bot', booking_date='2030-01-15', booking_time='17:00',
                party_size=2, booking_name='Test Client', earliest='17:00', latest='17:00')
    dialogue = LiveDialogue(client, RuleBasedProvider(), form, tmp_path, notify)
    return dialogue, client, notifications


def event(text, id=10, sender_id=77):
    return SimpleNamespace(id=id, sender_id=sender_id, chat_id=sender_id, out=False, raw_text=text)


def test_full_transport_roundtrip_with_client_question(setup):
    d, telegram, notes = setup
    run(d.start())
    assert 'Test Client' in telegram.sent[0][1]
    run(d.receive(event('How many guests?')))
    assert '2 people' in telegram.sent[-1][1]
    run(d.receive(event('Any allergies?', id=11)))
    assert d.engine.state.pending_question
    count = len(telegram.sent)
    run(d.answer('No allergies.'))
    assert len(telegram.sent) == count + 1
    assert telegram.sent[-1][1] == 'No allergies.'
    run(d.receive(event('This is Test Bistro. Confirmed for two people on 2030-01-15 at 17:00.', id=12)))
    assert d.stopped
    assert 'Test Bistro' in telegram.sent[-1][1]
    assert not telegram.handlers
    outgoing = [row for row in d.store.messages() if row['direction'] == 'outgoing']
    assert all(row['delivery'] == 'sent' and row['telegram_message_id'] for row in outgoing)


def test_duplicate_and_foreign_messages_are_ignored(setup):
    d, telegram, _ = setup
    run(d.start())
    incoming = event('What time?')
    run(d.receive(incoming))
    count = len(telegram.sent)
    run(d.receive(incoming))
    run(d.receive(event('What time?', id=11, sender_id=88)))
    assert len(telegram.sent) == count


def test_stop_prevents_all_further_sends(setup):
    d, telegram, _ = setup
    run(d.start())
    d.stop()
    run(d.receive(event('What time?')))
    assert len(telegram.sent) == 1
    with pytest.raises(ValueError):
        run(d.answer('Anything'))


def test_delivery_error_does_not_retry(setup):
    d, telegram, _ = setup
    telegram.fail = True
    with pytest.raises(OSError):
        run(d.start())
    assert d.stopped and not telegram.handlers
    assert d.store.messages()[-1]['delivery'] == 'unknown'


def test_failed_notification_does_not_duplicate_send(setup):
    d, telegram, _ = setup
    async def fail(text):
        raise OSError('mock Bot API disconnect')
    d.notify = fail
    run(d.start())
    assert len(telegram.sent) == 1


def test_allowed_targets_only(tmp_path):
    with pytest.raises(ValueError):
        LiveDialogue(None, None, {'manager': '@unexpected'}, tmp_path, None)


def test_assignment_target_resolves_without_real_network(setup):
    d, telegram, _ = setup
    d.target_handle = '@vlad_dev_7'
    run(d.start())
    assert d.target.username == 'vlad_dev_7'
    assert len(telegram.sent) == 1


def test_groq_decision_bridge_uses_full_client_context(setup):
    d, telegram, notes = setup
    class MockGroq:
        async def decide(self, request, history):
            assert request.booking_name == 'Test Client'
            assert history[0]['role'] == 'agent'
            return dict(action='ask_client', message='Есть ли аллергии?', reason='Missing client fact',
                        restaurant=None, date=None, time=None, guests=None)
    d.engine.provider = MockGroq()
    run(d.start())
    run(d.receive(event('Do you have allergies?')))
    assert len(telegram.sent) == 1
    assert any('Есть ли аллергии?' in text for text in notes)
    run(d.answer('No allergies.'))
    assert telegram.sent[-1][1] == 'No allergies.'


def test_panel_launch_and_shared_account_lock(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from booking_agent.commercial_control_bot import CommercialControlPanel
    import booking_agent.commercial_control_bot as panel_module
    class IsolatedDialogue(LiveDialogue):
        def __init__(self, client, provider, form, directory, notify):
            super().__init__(client, provider, form, tmp_path, notify)
    monkeypatch.setattr(panel_module, 'LiveDialogue', IsolatedDialogue)
    monkeypatch.setattr(panel_module, 'configured_provider', lambda: RuleBasedProvider())
    panel = object.__new__(CommercialControlPanel)
    panel.launch_lock = asyncio.Lock()
    panel.local_dialogues = {}
    panel.bot = SimpleNamespace(send_message=AsyncMock())
    panel.registry = SimpleNamespace(delete_form=lambda chat_id: None, save_route=lambda *args: None)
    telegram = FakeTelegram()
    panel.runtime = lambda chat_id, mode: SimpleNamespace(client=telegram)
    panel.connected = AsyncMock(return_value=True)
    message = SimpleNamespace(chat=SimpleNamespace(id=101), answer=AsyncMock())
    form = dict(manager='@vlad_dev_7', recipient_entered_manually=True, sender_mode='representative', booking_date='2099-01-15',
                booking_time='17:00', party_size=2, booking_name='Test Client', earliest='17:00', latest='17:00')
    run(panel.launch_live(message, form))
    assert len(telegram.sent) == 1
    # Another client must not start a simultaneous dialogue on the same account.
    other = SimpleNamespace(chat=SimpleNamespace(id=202), answer=AsyncMock())
    run(panel.launch_live(other, form))
    assert len(telegram.sent) == 1
    assert 'уже ведёт' in other.answer.call_args.args[0]


def test_panel_rejects_wrong_recipient_before_connect():
    import asyncio
    from unittest.mock import AsyncMock
    from booking_agent.commercial_control_bot import CommercialControlPanel
    panel = object.__new__(CommercialControlPanel)
    panel.launch_lock = asyncio.Lock()
    panel.connected = AsyncMock()
    message = SimpleNamespace(chat=SimpleNamespace(id=101), answer=AsyncMock())
    run(panel.launch_live(message, {'manager': '@unexpected'}))
    panel.connected.assert_not_called()


def test_old_automatic_draft_cannot_send():
    import asyncio
    from unittest.mock import AsyncMock
    from booking_agent.commercial_control_bot import CommercialControlPanel
    panel = object.__new__(CommercialControlPanel)
    panel.launch_lock = asyncio.Lock()
    panel.connected = AsyncMock()
    message = SimpleNamespace(chat=SimpleNamespace(id=101), answer=AsyncMock())
    run(panel.launch_live(message, {'manager': '@vlad_dev_7'}))
    panel.connected.assert_not_called()


def test_panel_answer_survives_restart(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from aiogram import Router
    from booking_agent.commercial_control_bot import CommercialControlPanel
    from booking_agent.database import ClientRegistry
    import booking_agent.commercial_control_bot as module
    monkeypatch.setattr(module, 'configured_provider', lambda: RuleBasedProvider())
    d, telegram, notes = None, FakeTelegram(), []
    async def notify(text):
        notes.append(text)
    # Use the real project's resolved client root; isolate storage via monkeypatched Path.
    def isolated_path(value):
        return tmp_path / value
    monkeypatch.setattr(module, 'Path', isolated_path)
    directory = tmp_path/'data/live-dialogues/101'
    form = dict(manager='@project_sync_bot', booking_date='2030-01-15', booking_time='17:00',
                party_size=2, booking_name='Test Client', earliest='17:00', latest='17:00')
    d = LiveDialogue(telegram, RuleBasedProvider(), form, directory, notify)
    run(d.start())
    run(d.receive(event('Any allergies?')))
    registry = ClientRegistry(tmp_path/'registry.db')
    registry.save_route(101, d.directory, 'personal')
    registry.wait_for_answer(101, d.last_incoming_id)
    d.stop('interrupted')
    panel = object.__new__(CommercialControlPanel)
    panel.registry = registry
    panel.router = Router()
    panel.local_dialogues = {}
    panel.answering_manager = set()
    panel.pending_disconnect = set()
    panel.bot = SimpleNamespace(send_message=AsyncMock())
    panel.connected = AsyncMock(return_value=True)
    panel.runtime = lambda *args: SimpleNamespace(client=telegram)
    panel._register_handlers()
    handler = next(h.callback for h in panel.router.message.handlers if h.callback.__name__ == 'input_text')
    message = SimpleNamespace(chat=SimpleNamespace(id=101, type='private'),
        from_user=SimpleNamespace(id=101, full_name='Test Client'),
        text='No allergies.', answer=AsyncMock())
    count = len(telegram.sent)
    run(handler(message))
    assert len(telegram.sent) == count + 1
    assert telegram.sent[-1][1] == 'No allergies.'
    assert registry.route(101)['waiting_answer'] == 0


def test_stale_client_answer_cannot_answer_new_question(setup):
    d, telegram, _ = setup
    run(d.start())
    run(d.receive(event('Any allergies?', id=10)))
    run(d.receive(event('Do you need a highchair?', id=11)))
    count = len(telegram.sent)
    with pytest.raises(ValueError, match='новое сообщение'):
        run(d.answer('No allergies.', expected_question_id=10))
    assert len(telegram.sent) == count


def test_final_refusal_is_sent_before_handler_removed(setup):
    d, telegram, notes = setup
    run(d.start())
    run(d.receive(event('Unfortunately, we are fully booked for the rest of the evening.')))
    assert 'Thank you for letting me know' in telegram.sent[-1][1]
    assert d.stopped and not telegram.handlers
    assert d.store.messages()[-1]['delivery'] == 'sent'


def test_opening_hides_private_flexibility_window(setup):
    d, telegram, _ = setup
    d.engine.state.request.earliest = '16:30'
    d.engine.state.request.latest = '17:30'
    run(d.start())
    opening = telegram.sent[0][1]
    assert 'at 5 pm' in opening
    assert '4:30' not in opening and '5:30' not in opening
    assert 'London time' not in opening and 'exactly' not in opening
    assert 'restaurant name' not in opening

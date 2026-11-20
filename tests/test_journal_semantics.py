from booking_agent.live_dialogue import LiveDialogue
from test_live_dialogue import FakeTelegram, event, run
from test_llm_policy import FakeLLM, decision


def test_five_message_journal_distinguishes_name_from_confirmation(tmp_path):
    client = FakeTelegram()
    provider = FakeLLM(decision('confirm', date='2026-11-20', time='17:00', guests=2, restaurant=None))
    async def notify(text):
        pass
    form = dict(manager='@project_sync_bot', booking_date='2026-11-20', booking_time='17:00',
                party_size=2, booking_name='Test Client', earliest='17:00', latest='17:00')
    dialogue = LiveDialogue(client, provider, form, tmp_path, notify)
    run(dialogue.start())
    run(dialogue.receive(event('Your table for two is confirmed for 20 November 2026 at 5 pm.')))
    provider.decision = decision('confirm', date='2026-11-20', time='17:00', guests=2,
                                restaurant='Test Bistro', reason='Manager confirmed booking')
    run(dialogue.receive(event('This is Test Bistro.', id=11)))
    rows = dialogue.store.messages()
    assert len(rows) == 5
    assert 'Клиент запросил столик' in rows[0]['understood']
    assert 'подтвердил' in rows[1]['understood']
    assert 'название ресторана неизвестно' in rows[1]['understood']
    assert rows[3]['understood'] == 'Менеджер сообщил недостающее название ресторана: Test Bistro.'
    assert 'Сохранить название' in rows[3]['decision']
    assert 'Все обязательные условия' in rows[4]['understood']
    assert rows[4]['understood'] != rows[3]['understood']
    assert rows[4]['decision'] != rows[3]['decision']
    assert 'итоговое подтверждение' in rows[4]['decision']
    for row in rows:
        if row['direction'] == 'outgoing':
            assert row['delivery'] == 'sent'
            assert row['understood'] != row['decision']


def test_export_does_not_rewrite_historical_journal(tmp_path):
    from booking_agent.store import StateStore
    store = StateStore(tmp_path/'state.json', tmp_path/'decisions.jsonl')
    store.log('incoming', 'This is Test Bistro.', 'Legacy summary', 'Legacy decision')
    before = store.journal_path.read_bytes()
    store.export_text()
    assert store.journal_path.read_bytes() == before


def test_outgoing_never_changes_interpretation_of_manager_message(tmp_path):
    from booking_agent.store import StateStore
    store = StateStore(tmp_path/'state.json', tmp_path/'decisions.jsonl')
    identifier = store.log('incoming', 'This is Test Bistro.',
                           'Менеджер сообщил название.', 'Сохранить название.')
    before = store.messages()[0].copy()
    store.log('outgoing', 'Thank you. I confirm the booking.',
              'Все условия известны.', 'Отправить подтверждение.')
    assert store.messages()[0] == before
    assert store.messages()[0]['id'] == identifier
    assert store.messages()[1]['decision'] != before['decision']

import io
from types import SimpleNamespace
import wave

import httpx
import pytest

from booking_agent.voice import GroqSpeech, VoiceError, chunks, join_wav
from test_live_dialogue import setup, run, event


class Speech:
    mode = 'auto'
    tts_available = True

    async def synthesize(self, text):
        return b'OggS-mock-audio'

    async def transcribe(self, audio):
        return 'How many guests?'


def test_chunking_never_loses_words():
    text = 'Could you please confirm the booking for two? ' * 12
    parts = chunks(text)
    assert len(parts) > 1
    assert all(len(part) <= 200 for part in parts)
    assert ' '.join(parts).split() == text.split()
    with pytest.raises(VoiceError):
        chunks('x' * 201)


def test_join_wave_headers_and_frames():
    def wav(rate):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0\0' * 100)
        return buffer.getvalue()
    with wave.open(io.BytesIO(join_wav([wav(24000), wav(24000)])), 'rb') as result:
        assert result.getnframes() == 200
    with pytest.raises(VoiceError):
        join_wav([wav(24000), wav(16000)])


def test_voice_send_has_one_delivery_and_no_text_duplicate(setup):
    d, client, notes = setup
    run(d.start())
    d.speech, d.reply_with_voice = Speech(), True
    sent_files = []
    async def send_file(target, audio, **options):
        sent_files.append((audio.read(), options))
        return SimpleNamespace(id=99)
    client.send_file = send_file
    reply = d.engine._reply('Thank you.', 'test')
    run(d.send(reply))
    assert len(client.sent) == 1
    assert len(sent_files) == 1 and sent_files[0][1]['voice_note']
    assert d.store.messages()[-1]['media'] == 'voice'
    assert d.store.messages()[-1]['telegram_message_id'] == 99


def test_synthesis_failure_falls_back_before_send(setup):
    d, client, notes = setup
    run(d.start())
    class BrokenSpeech(Speech):
        async def synthesize(self, text):
            raise VoiceError('mock quota')
    d.speech, d.reply_with_voice = BrokenSpeech(), True
    reply = d.engine._reply('Thank you.', 'test')
    run(d.send(reply))
    assert client.sent[-1][1] == reply
    assert d.store.messages()[-1]['media'] == 'text'
    assert any('Озвучивание недоступно' in note for note in notes)


def test_voice_delivery_failure_never_falls_back_to_text(setup):
    d, client, notes = setup
    run(d.start())
    d.speech, d.reply_with_voice = Speech(), True
    async def send_file(*args, **kwargs):
        raise OSError('delivery unknown')
    client.send_file = send_file
    reply = d.engine._reply('Thank you.', 'test')
    with pytest.raises(OSError):
        run(d.send(reply))
    assert d.stopped and len(client.sent) == 1
    assert d.store.messages()[-1]['delivery'] == 'unknown'


def test_explicit_text_format_overrides_always_voice(setup):
    d, client, notes = setup
    d.message_format, d.speech = 'text', Speech()
    d.speech.mode = 'always'
    run(d.start())
    assert len(client.sent) == 1
    assert d.store.messages()[-1]['media'] == 'text'


def test_explicit_voice_opening_and_restore(setup, monkeypatch):
    from booking_agent.live_dialogue import LiveDialogue
    d, client, notes = setup
    d.message_format, d.speech = 'voice', Speech()
    d.speech.mode = 'always'
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    sent = []
    async def send_file(target, audio, **kwargs):
        sent.append(audio.read())
        return SimpleNamespace(id=99)
    client.send_file = send_file
    run(d.start())
    assert len(sent) == 1 and not client.sent
    d.stop('interrupted')
    restored = run(LiveDialogue.restore(client, d.engine.provider, d.directory, d.notify))
    assert restored.message_format == 'voice'
    assert restored.speech.mode == 'always'
    assert len(sent) == 1  # Restore never resends the opening.


def test_review_offers_both_send_formats():
    from booking_agent.booking_form import prompt, LIVE_CONFIRM, VOICE_CONFIRM
    form = dict(step='review', restaurant_name='Unknown restaurant', booking_name='Test Client',
                party_size=2, booking_date='2030-01-15', booking_time='17:00', earliest='17:00',
                latest='17:00', manager='@project_sync_bot', sender_mode='personal')
    text, buttons = prompt(form)
    assert buttons[:2] == [LIVE_CONFIRM, VOICE_CONFIRM]
    assert 'первому сообщению' in text


@pytest.mark.asyncio
async def test_incoming_voice_drives_same_booking_engine(setup):
    d, client, notes = setup
    await d.start()
    d.speech = Speech()
    async def download_media(**kwargs):
        return b'OggS-mock-audio'
    async def send_file(*args, **kwargs):
        return SimpleNamespace(id=99)
    client.send_file = send_file
    incoming = event('', id=12)
    incoming.voice = True
    incoming.file = SimpleNamespace(size=100, duration=3)
    incoming.download_media = download_media
    await d.receive(incoming)
    rows = d.store.messages()
    assert rows[-2]['message'] == 'How many guests?'
    assert rows[-2]['media'] == 'voice'
    assert rows[-1]['media'] == 'voice'
    assert '2 people' in rows[-1]['message']
    exported = d.store.export_text().read_text(encoding='utf-8')
    assert 'Формат: голосовое; текст — расшифровка' in exported
    assert 'Формат: голосовое; текст — озвученный ответ' in exported


@pytest.mark.asyncio
async def test_unreadable_voice_is_not_sent_to_llm(setup):
    d, client, notes = setup
    await d.start()
    d.speech = Speech()
    incoming = event('fake caption', id=12)
    incoming.voice = True
    incoming.file = SimpleNamespace(size=99999999, duration=3)
    await d.receive(incoming)
    assert 'send that in writing' in client.sent[-1][1]
    assert not d.stopped
    assert 'fake caption' not in str(d.engine.state.messages)
    assert d.store.messages()[-2]['media'] == 'voice'


@pytest.mark.asyncio
async def test_stt_rejects_uncertain_audio(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return httpx.Response(200, request=httpx.Request('POST', 'https://example.test'),
                json={'text': 'Confirmed at five.', 'duration': 3,
                      'segments': [{'no_speech_prob': .99, 'avg_logprob': -.2}]})
    monkeypatch.setattr('booking_agent.voice.httpx.AsyncClient', Client)
    with pytest.raises(VoiceError, match='Uncertain'):
        await GroqSpeech('test-key').transcribe(b'OggS')

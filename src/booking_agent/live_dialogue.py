"""A single booking conversation over an already-authorised Telethon client."""
import asyncio
import json
import io
from pathlib import Path
from uuid import uuid4

from telethon import events

from .engine import BookingEngine
from .models import BookingRequest, ConversationState, Status
from .store import StateStore
from .display import human_date, human_time, guests
from .voice import configured_speech, MAX_BYTES, MAX_SECONDS, VoiceError

ALLOWED_TARGETS = {'@project_sync_bot', '@vlad_dev_7'}
TERMINAL = {Status.CONFIRMED, Status.CANCELLED, Status.ESCALATED}


class LiveDialogue:
    def __init__(self, client, provider, form, directory: Path, notify):
        target = form['manager'].lower()
        if target not in ALLOWED_TARGETS:
            raise ValueError('Unsupported booking target')
        self.client, self.notify, self.target_handle = client, notify, target
        self.directory = directory / uuid4().hex
        self.store = StateStore(self.directory/'state.json', self.directory/'decisions.jsonl')
        request = BookingRequest(
            restaurant_name=form.get('restaurant_name', 'Unknown restaurant'),
            booking_date=form['booking_date'], booking_time=form['booking_time'],
            party_size=form['party_size'], booking_name=form['booking_name'],
            earliest=form['earliest'], latest=form['latest'],
        )
        self.engine = BookingEngine(ConversationState(request), provider, self.store)
        self.lock = asyncio.Lock()
        self.stopped = False
        self.target = None
        self.last_incoming_id = 0
        self.handler_registered = False
        self.account_id = None
        self.message_format = form.get('message_format', 'auto')
        if self.message_format not in {'text', 'voice', 'auto'}:
            raise ValueError('Unsupported message format')
        self.speech = configured_speech('always' if self.message_format == 'voice' else None)
        self.reply_with_voice = False

    def checkpoint(self, phase):
        path = self.directory/'transport.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(phase=phase, target=self.target_handle,
            account_id=self.account_id, last_incoming_id=self.last_incoming_id,
            message_format=self.message_format)), encoding='utf-8')
        temporary.replace(path)

    async def tell(self, text):
        # A failed notification must not resend a restaurant message.
        try:
            await self.notify(text)
        except Exception:
            self.store.log('notification_error', '', 'Panel notification failed', 'Do not repeat external send')

    async def start(self):
        async with self.lock:
            self.target = await self.client.get_entity(self.target_handle)
            if '@' + (getattr(self.target, 'username', '') or '').lower() != self.target_handle:
                raise ValueError('Resolved recipient does not match approved target')
            me = await self.client.get_me()
            self.account_id = me.id
            self.client.add_event_handler(self.receive, events.NewMessage(incoming=True, from_users=self.target))
            self.handler_registered = True
            r = self.engine.state.request
            opening = (f'Good afternoon. Could I book a table for {guests(r.party_size)} on {human_date(r.booking_date)} '
                       f'at {human_time(r.booking_time)}, under {r.booking_name}?')
            self.engine.state.status = Status.AWAITING_REPLY
            self.engine._record_outgoing(opening, 'Подготовить первый запрос ресторану по согласованной заявке клиента.')
            self.store.save(self.engine.state)
            await self.send(opening)
            await self.tell(f'✅ Запрос отправлен в {self.target_handle}. Ответы появятся здесь.')

    async def send(self, text):
        message_id = self.store.last_outgoing_id
        if self.stopped:
            return False
        audio = None
        if (self.message_format != 'text' and self.speech and self.speech.tts_available
                and (self.speech.mode == 'always' or self.reply_with_voice)):
            try:
                audio = io.BytesIO(await self.speech.synthesize(text))
                audio.name = 'reply.ogg'
            except Exception:
                self.store.log('voice_error', '', 'Озвучивание недоступно', 'Отправить подготовленный ответ текстом')
                await self.tell('⚠️ Озвучивание недоступно. Ответ отправлю текстом.')
        if self.stopped:
            return False
        self.store.annotate(message_id, media='voice' if audio else 'text')
        self.checkpoint('sending')
        try:
            if audio:
                delivered = await self.client.send_file(self.target, audio, voice_note=True, mime_type='audio/ogg')
            else:
                delivered = await self.client.send_message(self.target, text, parse_mode=None)
        except Exception:
            self.store.delivery(message_id, 'unknown')
            self.stop('delivery_unknown')
            await self.tell('⚠️ Telegram не подтвердил отправку. Диалог остановлен; проверьте чат перед повторным запуском.')
            raise
        self.store.delivery(message_id, 'sent', delivered.id)
        self.checkpoint('waiting')
        return True

    async def receive(self, event):
        async with self.lock:
            if (self.stopped or not self.target or event.sender_id != self.target.id
                    or event.chat_id != self.target.id or getattr(event, 'out', False)
                    or event.id <= self.last_incoming_id):
                return
            self.last_incoming_id = event.id
            self.checkpoint('processing')
            try:
                is_voice = bool(getattr(event, 'voice', None))
                self.reply_with_voice = is_voice
                if is_voice:
                    try:
                        if not self.speech:
                            raise VoiceError('Voice disabled')
                        size = getattr(getattr(event, 'file', None), 'size', None)
                        duration = getattr(getattr(event, 'file', None), 'duration', None)
                        if size is None or size > MAX_BYTES or (duration and duration > MAX_SECONDS):
                            raise VoiceError('Voice exceeds supported limits')
                        async with asyncio.timeout(45):
                            audio = await event.download_media(file=bytes)
                        incoming = await self.speech.transcribe(audio)
                    except Exception:
                        self.store.log('incoming', '[Голосовое сообщение; расшифровка недоступна]',
                            'Содержание голосового не установлено', 'Попросить собеседника написать текстом')
                        self.store.annotate(self.store.input_record()['id'], media='voice', telegram_message_id=event.id)
                        self.reply_with_voice = False
                        reply = self.engine._reply('Sorry, I could not make out your voice message. Could you please send that in writing?',
                            'Не угадывать содержание аудио; запросить текстовую версию')
                        await self.send(reply)
                        await self.tell('⚠️ Голосовое не распознано надёжно. Попросил собеседника написать текстом.')
                        return
                else:
                    incoming = event.raw_text or '[Non-text message; ask client to review the Telegram chat]'
                await self.tell(('🎙 Собеседник (расшифровка):\n' if is_voice else '📩 Собеседник:\n') + incoming[:3000])
                reply = await self.engine.receive(incoming)
                record = self.store.input_record()
                if record:
                    self.store.annotate(record['id'], media='voice' if is_voice else 'text', telegram_message_id=event.id)
                if self.stopped:
                    return
                if reply:
                    await self.send(reply)
                    await self.tell('💬 Агент:\n' + reply)
                elif self.engine.state.pending_question:
                    self.checkpoint('waiting_client')
                    await self.tell('❓ ' + (self.engine.state.escalation_reason or incoming)[:2500]
                                    + '\nНажмите «✍️ Ответить менеджеру». Ответ будет передан без изменений.')
                if self.engine.state.status in TERMINAL:
                    status = self.engine.state.status
                    self.stop('finished')
                    await self.tell({Status.CONFIRMED: '✅ Бронь подтверждена, итоговое сообщение отправлено.',
                                     Status.CANCELLED: 'Диалог завершён: отказ или отмена.',
                                     Status.ESCALATED: '⚠️ ' + (self.engine.state.escalation_reason or 'Диалог остановлен для ручной проверки.')}[status])
            except Exception:
                if not self.stopped:
                    self.stop('processing_error')
                    await self.tell('⚠️ Ошибка обработки. Диалог остановлен; проверьте чат и журнал.')

    async def answer(self, text, expected_question_id=None):
        async with self.lock:
            if self.stopped:
                raise ValueError('Диалог уже остановлен.')
            if expected_question_id is not None and expected_question_id != self.last_incoming_id:
                raise ValueError('Собеседник прислал новое сообщение. Нажмите «Ответить менеджеру» ещё раз и ответьте на актуальный вопрос.')
            reply = self.engine.answer_pending_question(text)
            await self.send(reply)
            await self.tell('✅ Ваш ответ отправлен собеседнику.')
            return reply

    @classmethod
    async def restore(cls, client, provider, directory, notify):
        """Reattach to a persisted waiting dialogue without sending an opening."""
        directory = Path(directory)
        meta = json.loads((directory/'transport.json').read_text(encoding='utf-8'))
        store = StateStore(directory/'state.json', directory/'decisions.jsonl')
        state = store.load()
        if not state or state.status in TERMINAL or meta['phase'] not in {'waiting', 'waiting_client', 'interrupted'}:
            raise ValueError('Диалог нельзя автоматически продолжить. Проверьте Telegram-чат и журнал: доставка могла остаться неподтверждённой.')
        if meta['target'] not in ALLOWED_TARGETS:
            raise ValueError('Недопустимый адресат сохранённого диалога.')
        me = await client.get_me()
        if me.id != meta['account_id']:
            raise ValueError('Сохранённая бронь привязана к другому аккаунту.')
        obj = cls.__new__(cls)
        obj.client, obj.notify, obj.target_handle = client, notify, meta['target']
        obj.directory, obj.store = directory, store
        obj.engine = BookingEngine(state, provider, store)
        obj.lock, obj.stopped = asyncio.Lock(), False
        obj.message_format = meta.get('message_format', 'auto')
        if obj.message_format not in {'text', 'voice', 'auto'}:
            raise ValueError('Unsupported saved message format')
        obj.speech = configured_speech('always' if obj.message_format == 'voice' else None)
        obj.reply_with_voice = False
        obj.last_incoming_id, obj.account_id = meta['last_incoming_id'], me.id
        obj.target = await client.get_entity(obj.target_handle)
        if '@' + (obj.target.username or '').lower() != obj.target_handle:
            raise ValueError('Адресат изменился.')
        client.add_event_handler(obj.receive, events.NewMessage(incoming=True, from_users=obj.target))
        obj.handler_registered = True
        return obj

    def stop(self, phase='stopped'):
        self.stopped = True
        if self.handler_registered:
            self.client.remove_event_handler(self.receive)
            self.handler_registered = False
        self.checkpoint(phase)

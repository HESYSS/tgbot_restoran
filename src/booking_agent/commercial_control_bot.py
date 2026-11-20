"""Multi-client onboarding panel backed by SQLite and Windows Credential Manager."""
from __future__ import annotations

import asyncio
import io
import os
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, BufferedInputFile, FSInputFile, InputMediaPhoto, KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import AuthTokenExpiredError, SessionPasswordNeededError

from .database import ClientRegistry
from .secure_session import SecureSessionVault
from . import booking_form
from .local_dialogue import LocalDialogue
from .providers import configured_provider
from .live_dialogue import LiveDialogue, ALLOWED_TARGETS
from .single_instance import SingleInstance

NEXT_QUESTION = '▶️ Следующий вопрос теста'
ANSWER_MANAGER = '✍️ Ответить менеджеру'
STOP_DIALOGUE = '⏹ Остановить тест'
EXPORT_LOG = '📄 Скачать журнал'


def dialogue_menu(live=False):
    rows = [] if live else [[KeyboardButton(text=NEXT_QUESTION)]]
    return ReplyKeyboardMarkup(keyboard=rows + [
        [KeyboardButton(text=ANSWER_MANAGER)],
        [KeyboardButton(text=STOP_DIALOGUE)],
        [KeyboardButton(text=EXPORT_LOG)],
    ], resize_keyboard=True, is_persistent=True)

NEW_BOOKING = "📝 Новое бронирование"

REPRESENTATIVE = "🏢 Представитель сервиса"
PERSONAL = "👤 Подключить Telegram"
USE_PERSONAL = "👤 Использовать мой Telegram"
DISCONNECT_PERSONAL = "🔓 Отключить мой Telegram"
CONFIRM_DISCONNECT = "✅ Подтвердить отключение"
CANCEL_DISCONNECT = "↩️ Отмена"
BACK = "← Главное меню"
STATUS = "📊 Мой статус"
ACTIVITY = "📜 История"
SAFETY = "🛡 Безопасность"
HOW_IT_WORKS = "ℹ️ Как это работает"
CHANGE_MODE = "🔄 Сменить режим"


def menu() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=NEW_BOOKING)], [KeyboardButton(text=STATUS), KeyboardButton(text=CHANGE_MODE)],
            [KeyboardButton(text=ACTIVITY), KeyboardButton(text=HOW_IT_WORKS)],
            [KeyboardButton(text=EXPORT_LOG), KeyboardButton(text=SAFETY)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Выберите действие…")


def mode_menu(has_personal_connection: bool) -> ReplyKeyboardMarkup:
    personal_button = USE_PERSONAL if has_personal_connection else PERSONAL
    rows = [[KeyboardButton(text=REPRESENTATIVE)], [KeyboardButton(text=personal_button)]]
    if has_personal_connection:
        rows.append([KeyboardButton(text=DISCONNECT_PERSONAL)])
    rows.append([KeyboardButton(text=BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Выберите отправителя…")


@dataclass
class LoginRuntime:
    client: TelegramClient
    vault: SecureSessionVault
    kind: str
    task: asyncio.Task | None = None


class CommercialControlPanel:
    def __init__(self, token: str) -> None:
        self.bot = Bot(token)
        self.dispatcher = Dispatcher()
        self.router = Router()
        self.dispatcher.include_router(self.router)
        self.registry = ClientRegistry(Path("data/booking-agent.db"))
        self.runtimes: dict[str, LoginRuntime] = {}
        self.pending_disconnect: set[int] = set()
        self.local_dialogues = {}
        self.answering_manager = set()
        self.launch_lock = asyncio.Lock()
        self.api_id, self.api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
        if not self.api_id or not self.api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        self._register_handlers()

    @staticmethod
    def session_key(chat_id: int, kind: str) -> str:
        return "representative-default" if kind == "representative" else f"client-{chat_id}"

    async def register(self, message: Message) -> bool:
        if message.chat.type != "private" or message.from_user is None:
            return False
        self.registry.register_client(message.chat.id, message.from_user.id, message.from_user.full_name or "Client")
        return True

    def runtime(self, chat_id: int, kind: str) -> LoginRuntime:
        key = self.session_key(chat_id, kind)
        if key not in self.runtimes:
            vault = SecureSessionVault(key)
            self.runtimes[key] = LoginRuntime(TelegramClient(vault.load(), int(self.api_id), self.api_hash), vault, kind)
        return self.runtimes[key]

    async def connected(self, chat_id: int, kind: str) -> bool:
        runtime = self.runtime(chat_id, kind)
        if not runtime.client.is_connected():
            await runtime.client.connect()
        return await runtime.client.is_user_authorized()

    async def begin_qr(self, message: Message, kind: str) -> None:
        chat_id, runtime = message.chat.id, self.runtime(message.chat.id, kind)
        if not runtime.client.is_connected():
            await runtime.client.connect()
        if await runtime.client.is_user_authorized():
            await self.activate(chat_id, runtime)
            await message.answer("✅ Этот аккаунт уже подключён.")
            return
        if runtime.task and not runtime.task.done():
            await message.answer("🔐 QR-код уже отправлен и действует около двух минут.")
            return
        qr = await runtime.client.qr_login()
        image = qrcode.make(qr.url)
        data = io.BytesIO()
        image.save(data, format="PNG")
        label = "аккаунта представителя" if kind == "representative" else "вашего аккаунта"
        qr_message = await message.answer_photo(
            BufferedInputFile(data.getvalue(), filename="telegram-login-qr.png"),
            caption=f"🔐 Одноразовый QR для {label}.\nОн действует около 30 секунд. Telegram → Настройки → Устройства → Подключить устройство. Не пересылайте QR.",
        )
        runtime.task = asyncio.create_task(self.wait_for_qr(chat_id, runtime, qr, qr_message))

    @staticmethod
    def _qr_image(url: str) -> BufferedInputFile:
        image = qrcode.make(url)
        data = io.BytesIO()
        image.save(data, format="PNG")
        return BufferedInputFile(data.getvalue(), filename="telegram-login-qr.png")

    async def wait_for_qr(self, chat_id: int, runtime: LoginRuntime, qr, qr_message: Message) -> None:
        try:
            for refresh in range(4):
                try:
                    await qr.wait(timeout=35)
                except (TimeoutError, AuthTokenExpiredError):
                    if refresh == 3:
                        await self.bot.send_message(chat_id, "⌛ Время подключения истекло. Нажмите «👤 Подключить Telegram», чтобы начать заново.")
                        return
                    await qr.recreate()
                    await qr_message.edit_media(InputMediaPhoto(
                        media=self._qr_image(qr.url),
                        caption=f"🔐 QR-код обновлён автоматически ({refresh + 2}/4).\nTelegram → Настройки → Устройства → Подключить устройство.",
                    ))
                    continue
                except SessionPasswordNeededError:
                    await self.bot.send_message(chat_id, "🔒 Нужна 2FA. Не отправляйте пароль боту; подтвердите подключение в официальном приложении.")
                    return
                except Exception:
                    await self.bot.send_message(chat_id, "⚠️ Подключение не завершено. Попробуйте создать новый QR-код.")
                    return
                await self.activate(chat_id, runtime)
                await self.bot.send_message(chat_id, "✅ Telegram подключён. Теперь отправителем выбран ваш аккаунт.", reply_markup=menu())
                return
        finally:
            try:
                await qr_message.delete()
            except TelegramBadRequest:
                pass

    async def activate(self, chat_id: int, runtime: LoginRuntime) -> None:
        runtime.vault.save(runtime.client.session)
        self.registry.save_connection(chat_id, runtime.kind,
                                      Path(f"credential-vault:{self.session_key(chat_id, runtime.kind)}"), active=True)
        self.registry.set_sender_mode(chat_id, runtime.kind)

    async def select_representative(self, message: Message) -> None:
        if not await self.connected(message.chat.id, "representative"):
            await message.answer("🏢 Аккаунт представителя пока не подключён. Его подключает администратор сервиса.")
            return
        self.registry.set_sender_mode(message.chat.id, "representative")
        await message.answer("🏢 Выбран сервисный представитель. Будущие сообщения будут отправляться с его аккаунта.", reply_markup=menu())

    async def select_personal(self, message: Message) -> None:
        if await self.connected(message.chat.id, "personal"):
            self.registry.set_sender_mode(message.chat.id, "personal")
            await message.answer("👤 Ваш аккаунт уже подключён и выбран как отправитель.", reply_markup=menu())
        else:
            await self.begin_qr(message, "personal")

    async def request_disconnect(self, message: Message) -> None:
        if not await self.connected(message.chat.id, "personal"):
            await message.answer("🔓 Личный Telegram сейчас не подключён к сервису.")
            return
        self.pending_disconnect.add(message.chat.id)
        await message.answer(
            "<b>🔓 Отключить Telegram?</b>\n\n"
            "Сервис потеряет доступ к вашему аккаунту. "
            "Ваш Telegram-аккаунт, чаты и другие устройства не будут затронуты.\n\n"
            "Нажмите «✅ Подтвердить отключение» или «↩️ Отмена».",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text=CANCEL_DISCONNECT)], [KeyboardButton(text=CONFIRM_DISCONNECT)]],
                resize_keyboard=True, is_persistent=True,
                input_field_placeholder="Подтвердите или отмените отключение…",
            ),
        )

    async def disconnect_personal(self, message: Message) -> None:
        chat_id = message.chat.id
        if chat_id not in self.pending_disconnect:
            return
        self.pending_disconnect.discard(chat_id)
        runtime = self.runtime(chat_id, "personal")
        if not runtime.client.is_connected():
            await runtime.client.connect()
        if await runtime.client.is_user_authorized():
            await runtime.client.log_out()
        runtime.vault.delete()
        await runtime.client.disconnect()
        self.runtimes.pop(self.session_key(chat_id, "personal"), None)
        self.registry.remove_connection(chat_id, "personal")
        self.registry.set_sender_mode(chat_id, "representative")
        await message.answer("✅ Доступ сервиса к вашему Telegram отключён. Ваш Telegram-аккаунт не удалён.", reply_markup=menu())

    async def status(self, message: Message) -> None:
        profile = self.registry.get_client(message.chat.id) or {}
        mode = "ваш аккаунт" if profile.get("sender_mode") == "personal" else "представитель сервиса"
        personal = "подключён" if await self.connected(message.chat.id, "personal") else "не подключён"
        representative = "подключён" if await self.connected(message.chat.id, "representative") else "не подключён"
        personal_mark = "✅" if personal == "подключён" else "○"
        representative_mark = "✅" if representative == "подключён" else "○"
        await message.answer(
            f"<b>📊 Ваш статус</b>\n\n"
            f"<b>Отправитель:</b> {mode}\n"
            f"{personal_mark} Ваш Telegram: {personal}\n"
            f"{representative_mark} Представитель сервиса: {representative}",
            parse_mode="HTML",
        )
        provider = configured_provider()
        await message.answer('Обработка диалога: ' + type(provider).__name__ +
            (' · ' + os.environ.get('GROQ_MODEL', 'openai/gpt-oss-20b') if type(provider).__name__ == 'GroqProvider' else ''))

    async def restore_dialogue(self, chat_id):
        if chat_id in self.local_dialogues:
            return self.local_dialogues[chat_id]
        route = self.registry.route(chat_id)
        if not route:
            return None
        directory = Path(route['directory']).resolve()
        root = (Path('data/live-dialogues') / str(chat_id)).resolve()
        if not directory.is_relative_to(root):
            raise ValueError('Путь сохранённой брони не принадлежит этому клиенту.')
        meta = json.loads((directory/'transport.json').read_text(encoding='utf-8'))
        if meta['phase'] in {'finished', 'stopped'}:
            self.registry.remove_route(chat_id)
            return None
        if not await self.connected(chat_id, route['sender_mode']):
            raise ValueError('Подключите аккаунт, с которого начата переписка, чтобы продолжить ответ.')
        async def notify(text):
            await self.notify_dialogue(chat_id, text)
        dialogue = await LiveDialogue.restore(self.runtime(chat_id, route['sender_mode']).client,
            configured_provider(), directory, notify)
        self.local_dialogues[chat_id] = dialogue
        return dialogue

    async def notify_dialogue(self, chat_id, text):
        dialogue = self.local_dialogues.get(chat_id)
        finished = dialogue is not None and dialogue.stopped
        if finished:
            self.registry.remove_route(chat_id)
            self.answering_manager.discard(chat_id)
        await self.bot.send_message(chat_id, text, reply_markup=menu() if finished else None)

    async def show_onboarding(self, message: Message, welcome: bool, include_menu: bool = False) -> None:
        title = "<b>👋 Добро пожаловать!</b>" if welcome else "<b>ℹ️ Как работают режимы</b>"
        await message.answer(
            f"{title}\n\n"
            "Поможем договориться о столике в ресторане. Вы выбираете, от чьего имени будет идти переписка.\n\n"
            "<b>🏢 Представитель сервиса</b>\n"
            "Диалог ведёт сервисный аккаунт. Ваш личный Telegram не подключается.\n\n"
            "<b>👤 Подключить Telegram</b>\n"
            "Вы сканируете одноразовый QR в официальном приложении. Сообщения будут идти от вашего аккаунта.\n\n"
            "Чтобы выбрать отправителя, нажмите <b>«🔄 Сменить режим»</b>. Там же можно подключить или отключить ваш Telegram.\n\n"
            "<i>Смена режима сама по себе не отправляет сообщений ресторану.</i>",
            parse_mode="HTML",
            reply_markup=menu() if include_menu else None,
        )

    def _register_handlers(self) -> None:
        @self.router.message(CommandStart())
        async def start(message: Message) -> None:
            self.pending_disconnect.discard(message.chat.id)
            existed = self.registry.get_client(message.chat.id) is not None
            if not await self.register(message):
                return
            try:
                active = await self.restore_dialogue(message.chat.id)
            except (ValueError, OSError, KeyError):
                await message.answer('Сохранённый диалог требует проверки. Не создавайте дубликат: проверьте чат и скачайте журнал.')
                return
            if active and not active.stopped:
                await message.answer('Диалог продолжается. Здесь можно ответить собеседнику, скачать журнал или остановить агента.', reply_markup=dialogue_menu(isinstance(active, LiveDialogue)))
                return
            self.registry.delete_form(message.chat.id)
            if not existed:
                await self.show_onboarding(message, welcome=True, include_menu=True)
            else:
                profile = self.registry.get_client(message.chat.id) or {}
                mode = "ваш аккаунт" if profile.get("sender_mode") == "personal" else "представитель сервиса"
                await message.answer(f"👋 С возвращением! Текущий режим: <b>{mode}</b>.", parse_mode="HTML", reply_markup=menu())

        @self.router.message(F.text)
        async def input_text(message: Message) -> None:
            if not await self.register(message):
                return
            chat_id = message.chat.id
            if message.text == EXPORT_LOG:
                from .store import StateStore
                directory = Path('data/local-dialogues') / str(chat_id)
                journals = list(directory.glob('*/messages.jsonl'))
                journals += list((Path('data/live-dialogues') / str(chat_id)).glob('*/messages.jsonl'))
                if not journals:
                    await message.answer('Журнала пока нет. Сначала начните проверку диалога.')
                    return
                latest = max(journals, key=lambda path: path.stat().st_mtime)
                store = StateStore(latest.with_name('state.json'), latest.with_name('decisions.jsonl'))
                await message.answer_document(FSInputFile(store.export_text()),
                    caption='Журнал последнего диалога. sent — Telegram подтвердил отправку; simulated — локальная имитация.')
                return
            if message.text == STOP_DIALOGUE:
                self.registry.remove_route(chat_id)
            try:
                dialogue = await self.restore_dialogue(chat_id)
            except (ValueError, OSError, KeyError):
                await message.answer('Не удалось восстановить диалог. Ваш ответ не отправлен. Проверьте подключение, чат и журнал. «Остановить тест» сбросит ожидание.')
                return
            if dialogue and dialogue.stopped:
                self.registry.remove_route(chat_id)
                self.local_dialogues.pop(chat_id, None)
                dialogue = None
            if dialogue:
                if message.text == STOP_DIALOGUE:
                    if isinstance(dialogue, LiveDialogue):
                        dialogue.stop()
                    self.local_dialogues.pop(chat_id, None)
                    self.answering_manager.discard(chat_id)
                    await message.answer('Тест остановлен.', reply_markup=menu())
                    return
                if message.text == ANSWER_MANAGER:
                    if dialogue.engine.state.pending_question:
                        self.answering_manager.add(chat_id)
                        if isinstance(dialogue, LiveDialogue):
                            self.registry.wait_for_answer(chat_id, dialogue.last_incoming_id)
                        extra = (' Вместе с вашим текстом агент сообщит выбранное время.' if dialogue.engine.state.pending_reply_prefix else '')
                        offer = dialogue.engine.state.pending_time_offer
                        prompt = ('Выберите предложенное время (Лондон) или «Нет». Агент сообщит ваше решение ресторану по-английски.' if offer else
                            (f'Введите ответ по-английски. Ваш текст будет передан {dialogue.target_handle} без перевода.{extra} Для отмены нажмите «↩️ Отмена».' if isinstance(dialogue, LiveDialogue) else 'Введите ответ по-английски для локального теста.'))
                        choices = ([[KeyboardButton(text=time) for time in offer['times']], [KeyboardButton(text='Нет')]] if offer else [])
                        await message.answer(prompt,
                            reply_markup=ReplyKeyboardMarkup(keyboard=choices + [[KeyboardButton(text=CANCEL_DISCONNECT)], [KeyboardButton(text=STOP_DIALOGUE)]], resize_keyboard=True))
                    else:
                        await message.answer('Сейчас нет вопроса, ожидающего вашего ответа.')
                    return
                if message.text == NEXT_QUESTION:
                    if isinstance(dialogue, LiveDialogue):
                        await message.answer('Ожидаем сообщение собеседника.', reply_markup=dialogue_menu(True))
                        return
                    try:
                        question, reply = await dialogue.next_question()
                    except ValueError as error:
                        await message.answer(str(error))
                        return
                    await message.answer('🧪 Тестовый менеджер:\n' + question)
                    reason = dialogue.engine.state.escalation_reason or 'Нужны дополнительные сведения.'
                    await message.answer('💬 Агент:\n' + reply if reply else '❓ ' + reason + '\nНажмите «✍️ Ответить менеджеру».')
                    if dialogue.stopped:
                        self.local_dialogues.pop(chat_id, None)
                        await message.answer(f'Локальный сценарий завершён. Статус: {dialogue.engine.state.status.value}.', reply_markup=menu())
                    return
                route = self.registry.route(chat_id)
                if chat_id in self.answering_manager or (route and route['waiting_answer']):
                    if message.text == CANCEL_DISCONNECT:
                        self.answering_manager.discard(chat_id)
                        if route:
                            self.registry.wait_for_answer(chat_id, None)
                        await message.answer('Ответ отменён.', reply_markup=dialogue_menu(isinstance(dialogue, LiveDialogue)))
                        return
                    try:
                        reply = dialogue.answer(message.text, expected_question_id=route['question_id']) if isinstance(dialogue, LiveDialogue) and route else dialogue.answer(message.text)
                        if inspect.isawaitable(reply):
                            reply = await reply
                    except ValueError as error:
                        await message.answer(str(error))
                        return
                    except Exception:
                        await message.answer('Отправка не подтверждена. Проверьте чат и журнал перед повтором.')
                        return
                    self.answering_manager.discard(chat_id)
                    if route:
                        self.registry.wait_for_answer(chat_id, None)
                    await message.answer(('💬 Ответ отправлен:\n' if isinstance(dialogue, LiveDialogue) else '💬 Подготовленный ответ:\n') + reply, reply_markup=menu() if dialogue.stopped else dialogue_menu(isinstance(dialogue, LiveDialogue)))
                    return
                await message.answer('Используйте кнопки управления диалогом.', reply_markup=dialogue_menu(isinstance(dialogue, LiveDialogue)))
                return
            if message.text == NEW_BOOKING:
                profile = self.registry.get_client(message.chat.id)
                form = {"step": "name", "sender_mode": profile["sender_mode"]}
                self.registry.save_form(message.chat.id, form)
                await self.show_form(message, form)
                return
            if message.text in {BACK, CANCEL_DISCONNECT}:
                self.registry.delete_form(message.chat.id)
            elif (self.registry.load_form(message.chat.id) or {}).get("step") not in {None, "prepared"}:
                await self.handle_form(message)
                return
            if message.text != CONFIRM_DISCONNECT:
                self.pending_disconnect.discard(message.chat.id)
            if message.text in {BACK, CANCEL_DISCONNECT}:
                runtime = self.runtimes.get(self.session_key(message.chat.id, "personal"))
                if runtime and runtime.task and not runtime.task.done():
                    runtime.task.cancel()
                    await asyncio.gather(runtime.task, return_exceptions=True)
                await message.answer("Главное меню", reply_markup=menu())
            elif message.text == REPRESENTATIVE:
                await self.select_representative(message)
            elif message.text in {PERSONAL, USE_PERSONAL}:
                await self.select_personal(message)
            elif message.text == DISCONNECT_PERSONAL:
                await self.request_disconnect(message)
            elif message.text == CONFIRM_DISCONNECT:
                await self.disconnect_personal(message)
            elif message.text == STATUS:
                await self.status(message)
            elif message.text == ACTIVITY:
                events = self.registry.recent_events(message.chat.id)
                text = "\n".join(f"• {row['event_type']}: {row['detail']}" for row in events) or "Событий пока нет."
                await message.answer("<b>📜 История</b>\n\n" + text, parse_mode="HTML")
            elif message.text == HOW_IT_WORKS:
                await self.show_onboarding(message, welcome=False)
            elif message.text == CHANGE_MODE:
                profile = self.registry.get_client(message.chat.id) or {}
                current = "ваш аккаунт" if profile.get("sender_mode") == "personal" else "представитель сервиса"
                has_personal = await self.connected(message.chat.id, "personal")
                await message.answer(
                    f"<b>🔄 Смена режима</b>\n\nСейчас выбран: <b>{current}</b>.\n\n"
                    "Выберите способ отправки ниже. Подключённая личная сессия сохранится, пока вы не нажмёте «Отключить мой Telegram».",
                    parse_mode="HTML",
                    reply_markup=mode_menu(has_personal),
                )
            elif message.text == SAFETY:
                await message.answer("🛡 Подключение даёт сервису доступ к вашему Telegram. Его можно отозвать через «Сменить режим → Отключить мой Telegram» или в Telegram → Настройки → Устройства. Пароли и коды входа не отправляйте в этот чат.")
            else:
                await message.answer("Выберите действие кнопками ниже. Чтобы изменить отправителя, нажмите «🔄 Сменить режим».", reply_markup=menu())

    async def run(self) -> None:
        await self.bot.set_my_commands([BotCommand(command="start", description="Регистрация и выбор режима")])
        print("Commercial control bot started. Open it in Telegram and press /start.")
        try:
            await self.dispatcher.start_polling(self.bot)
        finally:
            for dialogue in self.local_dialogues.values():
                if isinstance(dialogue, LiveDialogue) and not dialogue.stopped:
                    dialogue.stop('interrupted')
            for runtime in self.runtimes.values():
                if runtime.client.is_connected():
                    await runtime.client.disconnect()
            await self.bot.session.close()

    async def show_form(self, message: Message, form: dict) -> None:
        text, buttons = booking_form.prompt(form)
        if form['step'] == 'review':
            try:
                if await self.connected(message.chat.id, form['sender_mode']):
                    account = await self.runtime(message.chat.id, form['sender_mode']).client.get_me()
                    text += f"\n\nАккаунт отправителя: @{account.username}" if account.username else f"\n\nID аккаунта отправителя: {account.id}"
                    form['approved_account_id'] = account.id
                    self.registry.save_form(message.chat.id, form)
            except Exception:
                text += '\n\nНе удалось проверить подключение отправителя. Перед запуском оно будет проверено повторно.'
        if form["step"] == "name":
            saved = self.registry.booking_name(message.chat.id)
            if saved:
                buttons = [saved]
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=button)] for button in buttons] + [[KeyboardButton(text=CANCEL_DISCONNECT)]],
            resize_keyboard=True, is_persistent=True, input_field_placeholder="Введите ответ или выберите кнопку…",
        )
        await message.answer(text, reply_markup=keyboard)

    async def handle_form(self, message: Message) -> None:
        form = self.registry.load_form(message.chat.id)
        if not form:
            return
        value = message.text.strip()
        if form["step"] == "review":
            if value == booking_form.RESTART:
                form = {"step": "name", "sender_mode": form["sender_mode"]}
            elif value == booking_form.CONFIRM:
                self.local_dialogues[message.chat.id] = LocalDialogue(form, Path('data/local-dialogues') / str(message.chat.id), provider=configured_provider())
                self.registry.delete_form(message.chat.id)
                await message.answer('🧪 Локальная репетиция диалога. Внешним адресатам ничего не отправляется. Нажмите «Следующий вопрос теста».', reply_markup=dialogue_menu())
                return
            elif value in {booking_form.LIVE_CONFIRM, booking_form.VOICE_CONFIRM}:
                form['message_format'] = 'voice' if value == booking_form.VOICE_CONFIRM else 'text'
                await self.launch_live(message, form)
                return
            else:
                await self.show_form(message, form)
                return
        elif form["step"] == "prepared":
            self.registry.delete_form(message.chat.id)
            await message.answer("Заявка сохранена. Выберите действие в меню.", reply_markup=menu())
            return
        else:
            try:
                booking_form.advance(form, value, datetime.now(ZoneInfo("Europe/London")).date())
            except ValueError as error:
                await message.answer(str(error))
                return
            if "booking_name" in form:
                self.registry.save_booking_name(message.chat.id, form["booking_name"])
        if form['step'] == 'restaurant':
            form.update(restaurant_name='Unknown restaurant', step='manager')
        self.registry.save_form(message.chat.id, form)
        await self.show_form(message, form)

    async def launch_live(self, message, form):
        async with self.launch_lock:
            chat_id = message.chat.id
            target = form.get('manager', '')
            if target not in ALLOWED_TARGETS or not form.get('recipient_entered_manually'):
                await message.answer('Создайте новую заявку и вручную укажите адресата перед отправкой.')
                return
            deadline = datetime.fromisoformat(form['booking_date'] + 'T' + form['latest']).replace(tzinfo=ZoneInfo('Europe/London'))
            if deadline <= datetime.now(ZoneInfo('Europe/London')):
                await message.answer('Допустимое время уже прошло в Лондоне. Заполните заявку заново.')
                return
            mode = form['sender_mode']
            if not await self.connected(chat_id, mode):
                await message.answer('Выбранный аккаунт не подключён. Отмените заявку и подключите его через «Сменить режим».')
                return
            runtime = self.runtime(chat_id, mode)
            account = await runtime.client.get_me()
            if form.get('approved_account_id') is not None and account.id != form['approved_account_id']:
                await message.answer('Аккаунт отправителя изменился. Создайте заявку заново и проверьте сводку.')
                return
            if any(isinstance(d, LiveDialogue) and not d.stopped and d.account_id == account.id
                   for d in self.local_dialogues.values()):
                await message.answer('Этот аккаунт уже ведёт диалог. Сначала остановите его: заявки не будут смешиваться.')
                return
            async def notify(text):
                await self.notify_dialogue(chat_id, text)
            dialogue = LiveDialogue(runtime.client, configured_provider(), form, Path('data/live-dialogues') / str(chat_id), notify)
            self.local_dialogues[chat_id] = dialogue
            self.registry.save_route(chat_id, dialogue.directory, mode)
            dialogue.store.log('provider_selected', '', type(dialogue.engine.provider).__name__, 'Provider fixed for this booking')
            try:
                await dialogue.start()
            except Exception:
                dialogue.stop('start_error')
                self.registry.remove_route(chat_id)
                await message.answer(f'Не удалось подтвердить запуск. Проверьте чат {target} и журнал перед повтором.')
                return
            self.registry.delete_form(chat_id)
            await message.answer(f'Диалог с {target} запущен. Можно ответить на вопрос или остановить агента.', reply_markup=dialogue_menu(True))


async def main() -> None:
    load_dotenv(override=True)
    token = os.environ.get("CONTROL_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set CONTROL_BOT_TOKEN in your local .env")
    with SingleInstance(token):
        await CommercialControlPanel(token).run()


if __name__ == "__main__":
    asyncio.run(main())

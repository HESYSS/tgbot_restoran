"""Private Telegram control panel for the already-authorised client account.

The Bot API bot only provides controls and status. Restaurant messages continue to
be sent by the client's MTProto account through the existing Telethon session.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv
import qrcode
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from .engine import BookingEngine
from .models import BookingRequest, ConversationState, Status
from .providers import configured_provider
from .secure_session import SecureSessionVault
from .store import StateStore
from .single_instance import SingleInstance


def dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Test booking", callback_data="test"), InlineKeyboardButton(text="📊 Status", callback_data="status")],
        [InlineKeyboardButton(text="📜 Activity", callback_data="activity"), InlineKeyboardButton(text="🛡 Safety", callback_data="safety")],
        [InlineKeyboardButton(text="⏹ Stop active test", callback_data="stop")],
    ])


def control_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom menu, deliberately styled like a Telegram app menu."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔐 Подключить аккаунт")],
            [KeyboardButton(text="🧪 Тест бронирования")],
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📜 Активность")],
            [KeyboardButton(text="🛡 Безопасность"), KeyboardButton(text="⏹ Остановить тест")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие в меню…",
    )


class ControlPanel:
    def __init__(self, token: str, owner_id: int | None) -> None:
        self.bot = Bot(token)
        self.dispatcher = Dispatcher()
        self.router = Router()
        self.dispatcher.include_router(self.router)
        self.owner_path = Path("data/control-owner.json")
        saved_state = self._load_control_state()
        self.owner_id = owner_id or saved_state.get("owner_id")
        self.dashboard_message_id: int | None = saved_state.get("dashboard_message_id")
        self.menu_message_id: int | None = saved_state.get("menu_message_id")
        self.menu_version = saved_state.get("menu_version", 0)
        self.waiting_for_manager = False
        self.engine: BookingEngine | None = None
        self.manager_id: int | None = None
        self.manager_handle: str | None = None
        self.login_task: asyncio.Task | None = None
        self.client_events_enabled = False
        api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
        if not api_id or not api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        self.session_vault = SecureSessionVault("client-default")
        self.client = TelegramClient(self.session_vault.load(), int(api_id), api_hash)
        self._register_handlers()

    def _load_control_state(self) -> dict[str, int]:
        if not self.owner_path.exists():
            return {}
        try:
            value = json.loads(self.owner_path.read_text(encoding="utf-8"))
            return {key: int(value[key]) for key in ("owner_id", "dashboard_message_id", "menu_message_id", "menu_version") if value.get(key) is not None}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _save_control_state(self) -> None:
        self.owner_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.owner_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "owner_id": self.owner_id,
            "dashboard_message_id": self.dashboard_message_id,
            "menu_message_id": self.menu_message_id,
            "menu_version": self.menu_version,
        }), encoding="utf-8")
        temporary.replace(self.owner_path)

    async def ensure_menu(self, message: Message) -> None:
        """Create the persistent Reply Keyboard once; never duplicate it on /start."""
        if self.menu_message_id is not None and self.menu_version >= 2:
            return
        sent = await message.answer(
            "🏠 Restaurant Booking Agent\nПанель привязана к аккаунту клиента.",
            reply_markup=control_keyboard(),
        )
        self.menu_message_id = sent.message_id
        self.menu_version = 2
        self._save_control_state()

    async def refresh_dashboard(self, text: str = "🏠 Restaurant Booking Agent\nChoose an action below.") -> None:
        """Keep the control UI in one stable message rather than a scrolling chat."""
        if self.owner_id is None:
            return
        try:
            if self.dashboard_message_id is not None:
                await self.bot.edit_message_text(
                    text=text,
                    chat_id=self.owner_id,
                    message_id=self.dashboard_message_id,
                    reply_markup=None,
                )
            else:
                sent = await self.bot.send_message(self.owner_id, text)
                self.dashboard_message_id = sent.message_id
                self._save_control_state()
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
            sent = await self.bot.send_message(self.owner_id, text)
            self.dashboard_message_id = sent.message_id
            self._save_control_state()

    def allowed(self, chat_id: int) -> bool:
        return self.owner_id is not None and chat_id == self.owner_id

    async def private_only(self, message: Message | CallbackQuery) -> bool:
        chat_id = message.message.chat.id if isinstance(message, CallbackQuery) else message.chat.id
        if self.allowed(chat_id):
            return True
        if self.owner_id is None and isinstance(message, Message):
            client_account = await self.client.get_me()
            if client_account and message.from_user and message.from_user.id == client_account.id:
                self.owner_id = message.chat.id
                self._save_control_state()
                return True
            await message.answer("This control bot can only be paired by the Telegram account that owns the local client session.")
        return False

    def _register_handlers(self) -> None:
        @self.router.message(CommandStart())
        async def on_start(message: Message) -> None:
            if not await self.private_only(message):
                return
            await self.ensure_menu(message)
            if await self.client.is_user_authorized():
                await self.refresh_dashboard("📊 Готов к работе\nВыберите действие в постоянном меню снизу.")
            else:
                await self.refresh_dashboard("🔐 Аккаунт клиента не подключён\nНажмите «Подключить аккаунт», чтобы получить одноразовый QR-код.")

        @self.router.callback_query(F.data == "status")
        async def on_status(query: CallbackQuery) -> None:
            if not await self.private_only(query): return
            me = await self.client.get_me()
            active = f"Active test with {self.manager_handle}" if self.engine else "No active test"
            await self.refresh_dashboard(f"📊 Status\n👤 Client account: @{me.username or me.id}\n📌 {active}\n🤖 Control panel: private")
            await query.answer("Status updated")

        @self.router.callback_query(F.data == "test")
        async def on_test(query: CallbackQuery) -> None:
            if not await self.private_only(query): return
            self.waiting_for_manager = True
            await self.refresh_dashboard("🧪 Test booking\nSend the @username of a test account or test bot. No message will go to the real manager.")
            await query.answer("Waiting for test username")

        @self.router.message(F.text)
        async def on_text(message: Message) -> None:
            if not await self.private_only(message):
                return
            handle = message.text.strip()
            if handle == "🔐 Подключить аккаунт":
                await self.start_qr_login(message)
                return
            if handle == "🧪 Тест бронирования":
                if not await self.client.is_user_authorized():
                    await message.answer("🔐 Сначала подключите аккаунт клиента через QR-код.")
                    return
                self.waiting_for_manager = True
                await message.answer("🧪 Тест бронирования\nОтправьте @username тестового аккаунта или тестового бота. Реальному менеджеру ничего не будет отправлено.")
                return
            if handle == "📊 Статус":
                me = await self.client.get_me()
                if not me:
                    await message.answer("📊 Статус\nАккаунт клиента ещё не подключён. Нажмите «Подключить аккаунт».")
                else:
                    active = f"Активный тест с {self.manager_handle}" if self.engine else "Активного теста нет"
                    await message.answer(f"📊 Статус\n👤 Аккаунт клиента: @{me.username or me.id}\n📌 {active}\n🤖 Панель: приватная")
                return
            if handle == "📜 Активность":
                path = Path("data/notifications.jsonl")
                if not path.exists():
                    text = "📜 Активность\nСобытий пока нет."
                else:
                    lines = path.read_text(encoding="utf-8").splitlines()[-8:]
                    items = [json.loads(line) for line in lines]
                    text = "📜 Последние события\n" + "\n".join(f"• {item['title']}: {item['detail']}" for item in items)
                await message.answer(text[:4000])
                return
            if handle == "🛡 Безопасность":
                await message.answer("🛡 Безопасность\nПанель не пишет менеджеру. Аккаунт клиента контактирует только с явно выбранным тестовым адресатом. Запросы оплат, карт, ссылок и личных данных останавливают агента.")
                return
            if handle == "⏹ Остановить тест":
                self.engine, self.manager_id, self.manager_handle = None, None, None
                self.waiting_for_manager = False
                await message.answer("⏹ Тест остановлен\nНовых ответов отправляться не будет.")
                return
            if not self.waiting_for_manager:
                return
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            if not handle.startswith("@") or " " in handle:
                await self.bot.send_message(self.owner_id, "🧪 Тест бронирования\nОтправьте один username в формате @test_account.")
                return
            await self.start_test(handle, message)

        @self.router.callback_query(F.data == "stop")
        async def on_stop(query: CallbackQuery) -> None:
            if not await self.private_only(query): return
            self.engine, self.manager_id, self.manager_handle = None, None, None
            self.waiting_for_manager = False
            await self.refresh_dashboard("⏹ Test stopped\nNo more replies will be sent.")
            await query.answer("Test stopped")

        @self.router.callback_query(F.data == "activity")
        async def on_activity(query: CallbackQuery) -> None:
            if not await self.private_only(query): return
            path = Path("data/notifications.jsonl")
            if not path.exists():
                text = "📜 Activity\nNo notifications yet."
            else:
                lines = path.read_text(encoding="utf-8").splitlines()[-8:]
                items = [json.loads(line) for line in lines]
                text = "📜 Recent activity\n" + "\n".join(f"• {item['title']}: {item['detail']}" for item in items)
            await self.refresh_dashboard(text[:4000])
            await query.answer("Activity updated")

        @self.router.callback_query(F.data == "safety")
        async def on_safety(query: CallbackQuery) -> None:
            if not await self.private_only(query): return
            await self.refresh_dashboard("🛡 Safety\nThe control bot never sends messages to a manager. The client account only contacts a test account selected by you. Payment links, card details, deposits, and identity data stop the agent and require human action.")
            await query.answer("Safety rules shown")

    async def start_test(self, handle: str, message: Message) -> None:
        try:
            entity = await self.client.get_entity(handle)
        except Exception:
            await self.bot.send_message(self.owner_id, "🧪 Тест бронирования\nНе удалось найти этот username. Проверьте его и попробуйте снова.")
            return
        request = BookingRequest.for_today("", datetime.now(ZoneInfo("Europe/London")).date())
        store = StateStore(Path("data/control-state.json"), Path("data/decisions.jsonl"))
        self.engine = BookingEngine(ConversationState(request), configured_provider(), store)
        self.manager_id, self.manager_handle, self.waiting_for_manager = entity.id, handle, False
        opening = await self.engine.start()
        await self.client.send_message(entity, opening)
        await self.bot.send_message(self.owner_id, f"🧪 Тест начат: {handle}\nПервое сообщение отправлено с аккаунта клиента. Следите за ответами через «Активность».")

    async def start_qr_login(self, message: Message) -> None:
        """Authenticate through a short-lived QR, never through a code in bot chat."""
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            await message.answer(f"✅ Аккаунт уже подключён: @{me.username or me.id}")
            return
        if self.login_task and not self.login_task.done():
            await message.answer("🔐 QR-код уже отправлен. Отсканируйте его в течение двух минут.")
            return
        qr_login = await self.client.qr_login()
        image = qrcode.make(qr_login.url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        qr_message = await message.answer_photo(
            BufferedInputFile(buffer.getvalue(), filename="telegram-login-qr.png"),
            caption="🔐 Одноразовый QR-код (действует около двух минут).\nВ официальном Telegram откройте Настройки → Устройства → Подключить устройство и отсканируйте код. Не пересылайте это изображение.",
        )
        self.login_task = asyncio.create_task(self.wait_for_qr_login(qr_login, qr_message))

    async def wait_for_qr_login(self, qr_login, qr_message: Message) -> None:
        try:
            await qr_login.wait(timeout=120)
        except TimeoutError:
            await self.bot.send_message(self.owner_id, "⌛ QR-код истёк. Нажмите «Подключить аккаунт», чтобы получить новый.")
        except SessionPasswordNeededError:
            await self.bot.send_message(self.owner_id, "🔒 Для аккаунта требуется 2FA. Подтвердите подключение в официальном Telegram-приложении и создайте новый QR-код.")
        except Exception:
            await self.bot.send_message(self.owner_id, "⚠️ Не удалось завершить QR-подключение. Создайте новый QR-код и попробуйте снова.")
        else:
            self.session_vault.save(self.client.session)
            await self.enable_client_events()
            me = await self.client.get_me()
            await self.bot.send_message(self.owner_id, f"✅ Аккаунт подключён: @{me.username or me.id}. Можно запускать тест.")
        finally:
            try:
                await qr_message.delete()
            except TelegramBadRequest:
                pass

    async def enable_client_events(self) -> None:
        if not self.client_events_enabled:
            self.client.add_event_handler(self.on_manager_message, events.NewMessage(incoming=True))
            self.client_events_enabled = True

    async def on_manager_message(self, event) -> None:
        if not self.engine or event.sender_id != self.manager_id:
            return
        reply = await self.engine.receive(event.raw_text)
        if reply:
            await event.respond(reply)
        if self.owner_id:
            status = self.engine.state.status.value
            await self.bot.send_message(self.owner_id, f"📩 Ответ тестового адресата обработан\nСтатус: {status}\nТест: {self.manager_handle}")
        if self.engine.state.status in {Status.CONFIRMED, Status.CANCELLED, Status.ESCALATED}:
            self.engine = None
            self.manager_id = None

    async def run(self) -> None:
        await self.client.connect()
        if await self.client.is_user_authorized():
            await self.enable_client_events()
        await self.bot.set_my_commands([BotCommand(command="start", description="Открыть меню")])
        print("Control bot started. Open it in Telegram and press /start.")
        try:
            await self.dispatcher.start_polling(self.bot)
        finally:
            await self.client.disconnect()
            await self.bot.session.close()


async def main() -> None:
    load_dotenv()
    token = os.environ.get("CONTROL_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set CONTROL_BOT_TOKEN in your local .env")
    owner = os.environ.get("CONTROL_OWNER_ID")
    with SingleInstance(token):
        panel = ControlPanel(token, int(owner) if owner else None)
        await panel.run()


if __name__ == "__main__":
    asyncio.run(main())

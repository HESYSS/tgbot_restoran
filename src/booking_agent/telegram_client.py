from __future__ import annotations

import os
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from .engine import BookingEngine
from .notifications import Notifier, TelegramSelfNotifier
from .secure_session import SecureSessionVault


async def login_client(client: TelegramClient, notifier: Notifier) -> None:
    """Authorize locally; neither code, password nor session data is logged."""
    await client.connect()
    if await client.is_user_authorized():
        user = await client.get_me()
        await notifier.publish("info", "Session restored", f"Signed in as @{user.username or user.id}.")
        return

    await notifier.publish("warning", "New local Telegram login", "Telegram will request a phone number and one-time code locally. Never share either with anyone.")
    phone = input("Telegram phone number in international format: ").strip()
    await client.send_code_request(phone)
    code = input("Telegram verification code: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        password = getpass("Telegram two-step verification password: ")
        await client.sign_in(password=password)
    user = await client.get_me()
    await notifier.publish("info", "Local login complete", f"Session created for @{user.username or user.id}; its file stays on this computer.")


async def run_telegram(engine: BookingEngine, manager: str, session_key: str, dry_run: bool, notify_self: bool) -> None:
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your local .env")
    vault = SecureSessionVault(session_key)
    client = TelegramClient(vault.load(), int(api_id), api_hash)
    notifier: Notifier = Notifier(Path("data/notifications.jsonl"))
    await login_client(client, notifier)
    vault.save(client.session)
    if notify_self:
        notifier = TelegramSelfNotifier(Path("data/notifications.jsonl"), client)
        await notifier.publish("info", "Self notifications enabled", "Only this account's Saved Messages will receive status alerts.")
    entity = await client.get_entity(manager)
    if not dry_run:
        opening = await engine.start()
        try:
            delivered = await client.send_message(entity, opening)
        except Exception:
            engine.store.delivery(engine.store.last_outgoing_id, 'unknown')
            raise
        engine.store.delivery(engine.store.last_outgoing_id, 'sent', delivered.id)
        await notifier.publish("info", "Opening request sent", "The manager has received the first booking request.")
    else:
        await notifier.publish("warning", "Dry run active", "No message will be sent, even if the manager replies.")

    @client.on(events.NewMessage(from_users=entity))
    async def handler(event):
        await notifier.publish("info", "Manager message received", "A response was received and passed through the safety rules.")
        reply = await engine.receive(event.raw_text)
        if reply and not dry_run:
            try:
                delivered = await event.respond(reply)
            except Exception:
                engine.store.delivery(engine.store.last_outgoing_id, 'unknown')
                raise
            engine.store.delivery(engine.store.last_outgoing_id, 'sent', delivered.id)
            await notifier.publish("info", "Agent reply sent", "A safety-checked reply was sent from the client account.")
        elif engine.state.escalation_reason:
            await notifier.publish("warning", "Human action required", engine.state.escalation_reason)

    await notifier.publish("info", "Monitoring started", "Press Ctrl+C to stop. The agent never sends payment details or personal data.")
    await client.run_until_disconnected()

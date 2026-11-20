"""One-time local setup for the service representative Telegram account."""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

from .secure_session import SecureSessionVault


async def main() -> None:
    load_dotenv()
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
    vault = SecureSessionVault("representative-default")
    client = TelegramClient(vault.load(), int(api_id), api_hash)
    try:
        await client.start()
        vault.save(client.session)
        account = await client.get_me()
        print(f"Representative account connected: @{account.username or account.id}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

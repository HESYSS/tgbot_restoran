"""Session storage backed by the current Windows user's credential vault."""
from __future__ import annotations

import keyring
from keyring.errors import KeyringError
from telethon.sessions import StringSession


class SecureSessionVault:
    """Keep Telethon authorisation material out of files and SQLite databases."""

    SERVICE_NAME = "tgbot_restoran.telethon"

    def __init__(self, account_key: str) -> None:
        self.account_key = account_key

    def load(self) -> StringSession:
        try:
            saved = keyring.get_password(self.SERVICE_NAME, self.account_key)
        except KeyringError as error:
            raise RuntimeError("Windows Credential Manager is unavailable; Telegram sessions will not fall back to files.") from error
        return StringSession(saved) if saved else StringSession()

    def save(self, session: StringSession) -> None:
        encoded = session.save()
        if not encoded:
            raise RuntimeError("Cannot save an empty Telegram session")
        try:
            keyring.set_password(self.SERVICE_NAME, self.account_key, encoded)
        except KeyringError as error:
            raise RuntimeError("Could not save the session to Windows Credential Manager.") from error

    def delete(self) -> None:
        try:
            keyring.delete_password(self.SERVICE_NAME, self.account_key)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as error:
            raise RuntimeError("Could not remove the session from Windows Credential Manager.") from error

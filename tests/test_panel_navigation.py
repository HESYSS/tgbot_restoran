from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from booking_agent.commercial_control_bot import (
    CommercialControlPanel, menu, mode_menu, PERSONAL, DISCONNECT_PERSONAL,
    CONFIRM_DISCONNECT, CANCEL_DISCONNECT, USE_PERSONAL, BACK,
)


def labels(keyboard):
    return {button.text for row in keyboard.keyboard for button in row}


def test_account_actions_are_contextual():
    assert PERSONAL not in labels(menu())
    assert DISCONNECT_PERSONAL not in labels(menu())
    assert PERSONAL in labels(mode_menu(False))
    assert DISCONNECT_PERSONAL not in labels(mode_menu(False))
    assert PERSONAL not in labels(mode_menu(True))
    assert {DISCONNECT_PERSONAL, USE_PERSONAL, BACK} <= labels(mode_menu(True))


def test_disconnect_has_real_confirmation_buttons():
    panel = object.__new__(CommercialControlPanel)
    panel.connected = AsyncMock(return_value=True)
    panel.pending_disconnect = set()
    message = SimpleNamespace(chat=SimpleNamespace(id=123), answer=AsyncMock())
    # All I/O is mocked; drive this immediate coroutine without a Windows event loop.
    operation = panel.request_disconnect(message)
    with pytest.raises(StopIteration):
        operation.send(None)
    assert 123 in panel.pending_disconnect
    assert {CONFIRM_DISCONNECT, CANCEL_DISCONNECT} == labels(
        message.answer.call_args.kwargs['reply_markup']
    )

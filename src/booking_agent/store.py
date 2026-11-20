from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .models import ConversationState


class StateStore:
    def __init__(self, state_path: Path, log_path: Path) -> None:
        self.state_path, self.log_path = state_path, log_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path = log_path.with_name('messages.jsonl')
        self._journal_lock = RLock()
        self._current_input_id = None
        self.last_outgoing_id = None
        self.booking_id = str(log_path.parent.resolve())

    def load(self) -> ConversationState | None:
        if not self.state_path.exists():
            return None
        return ConversationState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))

    def save(self, state: ConversationState) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def log(self, direction: str, message: str, understood: str, decision: str) -> str | None:
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), "direction": direction,
                 "message": message, "understood": understood, "decision": decision}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        if direction in {'incoming', 'outgoing', 'client_input'}:
            message_id = uuid4().hex
            row = dict(event, id=message_id, booking_id=self.booking_id,
                       delivery='prepared' if direction == 'outgoing' else 'received',
                       telegram_message_id=None)
            with self._journal_lock:
                with self.journal_path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            if direction == 'outgoing':
                self.last_outgoing_id = message_id
            else:
                self._current_input_id = message_id
            return message_id
        if direction == 'decision' and self._current_input_id:
            self.annotate(self._current_input_id, understood=understood, decision=decision)
        return None

    def messages(self) -> list[dict]:
        with self._journal_lock:
            if not self.journal_path.exists():
                return []
            return [json.loads(line) for line in self.journal_path.read_text(encoding='utf-8').splitlines() if line]

    def input_understanding(self) -> str | None:
        if self._current_input_id:
            return next((row['understood'] for row in self.messages() if row['id'] == self._current_input_id), None)
        return None

    def input_record(self) -> dict | None:
        if self._current_input_id:
            return next((row for row in self.messages() if row['id'] == self._current_input_id), None)
        return None

    def annotate(self, message_id: str, **changes) -> None:
        with self._journal_lock:
            rows = self.messages()
            row = next((row for row in rows if row['id'] == message_id), None)
            if row is None:
                raise ValueError('Unknown journal message ID')
            row.update(changes)
            temporary = self.journal_path.with_suffix('.tmp')
            temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
            temporary.replace(self.journal_path)

    def delivery(self, message_id: str, status: str, telegram_message_id: int | None = None) -> None:
        if status not in {'sent', 'failed', 'simulated', 'unknown'}:
            raise ValueError('Invalid delivery status')
        if status == 'sent' and telegram_message_id is None:
            raise ValueError('Successful send must include the Telegram message ID')
        self.annotate(message_id, delivery=status, telegram_message_id=telegram_message_id,
                      delivery_updated_at=datetime.now(timezone.utc).isoformat())

    def export_text(self) -> Path:
        """One physical UTF-8 line per message; no internal event-only rows."""
        labels = {'incoming': 'Менеджер', 'outgoing': 'Агент', 'client_input': 'Клиент'}
        lines = []
        for row in self.messages():
            quoted = lambda value: json.dumps(value, ensure_ascii=False)
            media = ''
            if row.get('media') == 'voice':
                media = ' | Формат: голосовое; текст — ' + ('расшифровка' if row['direction'] == 'incoming' else 'озвученный ответ')
            lines.append(f"{row['timestamp']} | {labels[row['direction']]} | Статус: {row['delivery']} | "
                         f"Текст: {quoted(row['message'])} | Понял: {quoted(row['understood'])} | Решил: {quoted(row['decision'])}{media}")
        output = self.journal_path.with_name('decision-log.txt')
        output.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
        return output

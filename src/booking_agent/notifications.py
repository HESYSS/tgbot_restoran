from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Notification:
    level: str
    title: str
    detail: str


class Notifier:
    """Local, secret-free audit trail plus optional self-notifications in Telegram."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

    async def publish(self, level: str, title: str, detail: str) -> None:
        item = Notification(level, title, detail)
        print(f"[{level.upper()}] {title}: {detail}")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **asdict(item)}, ensure_ascii=False) + "\n")


class TelegramSelfNotifier(Notifier):
    def __init__(self, log_path: Path, client) -> None:
        super().__init__(log_path)
        self.client = client

    async def publish(self, level: str, title: str, detail: str) -> None:
        await super().publish(level, title, detail)
        await self.client.send_message("me", f"Booking agent — {level.upper()}\n{title}\n{detail}")

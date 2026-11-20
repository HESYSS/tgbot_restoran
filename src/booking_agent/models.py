from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum


class Status(StrEnum):
    NEW = "new"
    AWAITING_REPLY = "awaiting_reply"
    NEEDS_CLARIFICATION = "needs_clarification"
    CONFIRMED = "confirmed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


@dataclass
class BookingRequest:
    restaurant_name: str
    booking_date: str
    booking_time: str = "17:00"
    party_size: int = 2
    city: str = "London"
    booking_name: str = ""
    earliest: str | None = None
    latest: str | None = None

    @classmethod
    def for_today(cls, restaurant_name: str, london_today: date) -> "BookingRequest":
        name = restaurant_name.strip() or "Unknown restaurant"
        return cls(restaurant_name=name, booking_date=london_today.isoformat())


@dataclass
class ConversationState:
    request: BookingRequest
    status: Status = Status.NEW
    messages: list[dict[str, str]] = field(default_factory=list)
    escalation_reason: str | None = None
    pending_question: str | None = None
    pending_confirmation: dict | None = None
    pending_reply_prefix: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["status"] = self.status.value
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "ConversationState":
        return cls(
            request=BookingRequest(**value["request"]),
            status=Status(value["status"]),
            messages=value.get("messages", []),
            escalation_reason=value.get("escalation_reason"),
            pending_question=value.get("pending_question"),
            pending_confirmation=value.get("pending_confirmation"),
            pending_reply_prefix=value.get("pending_reply_prefix"),
        )

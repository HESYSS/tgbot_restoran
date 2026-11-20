from __future__ import annotations

import re

from .models import ConversationState, Status
from .providers import LLMProvider
from .store import StateStore
from .display import explicit_dates, human_date, human_time

RISK_WORDS = re.compile(r"\b(payment|card|deposit|bank|passport|id number|link)\b", re.I)
CANCEL_WORDS = re.compile(r"\b(cancelled|canceled|cannot accommodate|not available|fully booked)\b", re.I)
ACCEPT_WORDS = re.compile(r"\b(confirmed|booked|reserved|table is yours|we can accommodate)\b", re.I)
RESTAURANT_NAME = re.compile(
    r"\b(?:the restaurant(?:'s)? name is|this is|you are speaking with|welcome to)\s+([A-Za-z0-9&' .-]{2,80}?)(?:[.!?]|$)",
    re.I,
)


class BookingEngine:
    def __init__(self, state: ConversationState, provider: LLMProvider, store: StateStore) -> None:
        self.state, self.provider, self.store = state, provider, store

    async def start(self) -> str:
        if self.state.status is not Status.NEW:
            raise RuntimeError("Conversation already started")
        message = await self.provider.draft_reply(self.state.request, self.state.messages, None)
        self._record_outgoing(message, "Started courteous booking request")
        self.state.status = Status.AWAITING_REPLY
        self.store.save(self.state)
        return message

    async def receive(self, incoming: str) -> str | None:
        if self.state.status in {Status.CONFIRMED, Status.CANCELLED, Status.ESCALATED}:
            self.store.log("incoming", incoming, "Reply received after a terminal state", "Ignored; no further messages sent")
            return None
        self.state.messages.append({"role": "manager", "content": incoming})
        self.state.pending_reply_prefix = None
        self.store.log("incoming", incoming, "Manager reply received", "Classifying reply safely")
        from .restaurant import introduced_name
        name = introduced_name(incoming)
        if name:
            self.state.request.restaurant_name = name
            self.store.save(self.state)
            self.store.log('decision', '', 'Manager explicitly introduced restaurant', 'Remember restaurant name for the whole booking')
        if callable(getattr(self.provider, 'decide', None)):
            from .llm_policy import receive_with_llm
            return await receive_with_llm(self, incoming)
        restaurant = RESTAURANT_NAME.search(incoming)
        if restaurant and self.state.request.restaurant_name == "Unknown restaurant":
            self.state.request.restaurant_name = restaurant.group(1).strip()
            self.store.log("decision", "", "Restaurant name supplied by manager", "Saved manager-provided restaurant name")
        lower = incoming.lower()
        r = self.state.request
        if RISK_WORDS.search(incoming):
            return self._escalate("Manager requested payment, a link, or sensitive personal data.")
        times = re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", lower)
        normalized_times = [f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}" for t in times]
        if normalized_times and any(t != r.booking_time for t in normalized_times):
            lo, hi = r.earliest or r.booking_time, r.latest or r.booking_time
            if len(normalized_times) != 1 or not lo <= normalized_times[0] <= hi:
                return self._ask_client(incoming, "Manager proposed a different or ambiguous time")
            proposed = normalized_times[0]
            r.booking_time = proposed
            return self._reply(
                f"Thank you. {proposed} is within our availability. Could you please confirm the restaurant name, date and table for {r.party_size} at {proposed}?",
                "Requested explicit confirmation for an acceptable alternative time",
            )
        if RISK_WORDS.search(incoming):
            return self._escalate("Manager requested payment, a link, or sensitive personal data.")
        if CANCEL_WORDS.search(incoming):
            from .llm_policy import finish_declined
            return finish_declined(self, 'Final refusal received; politely closed without booking')
        if re.search(r"\b(not confirmed|not booked|not reserved|can't confirm|cannot confirm)\b", lower):
            return self._reply("Thank you. Could you please let me know whether a table is available for our requested date and time?", "Booking not confirmed")
        if re.search(r"\b(name|surname|who.*booking|book.*under)\b", lower) and "?" in incoming:
            if not r.booking_name:
                return self._ask_client(incoming, "Booking name is unknown")
            return self._reply(f"The booking is under {r.booking_name}, please.", "Answered with the supplied booking name")
        if re.search(r"\b(how many|number of guests|party size)\b", lower):
            return self._reply(f"A table for {r.party_size} people, please.", "Answered party size")
        if re.search(r"\b(what time|which time)\b", lower):
            return self._reply(f"{r.booking_time} on {r.booking_date}, please (London time).", "Answered requested time")
        if re.search(r"\b(what date|which date|what day|which day)\b", lower):
            return self._reply(f"On {r.booking_date}, please.", "Answered booking date")
        if "?" in incoming:
            return self._ask_client(incoming, "No verified answer available for manager's question")
        if ACCEPT_WORDS.search(incoming):
            if self.state.request.restaurant_name == "Unknown restaurant":
                self.state.status = Status.NEEDS_CLARIFICATION
                from .restaurant import REMINDER
                message = REMINDER
                self._record_outgoing(message, "Requested the restaurant name; it must not be guessed")
                self.store.save(self.state)
                return message
            missing = self._missing_confirmation_details(lower)
            if missing:
                self.state.status = Status.NEEDS_CLARIFICATION
                message = "Thank you. Could you please confirm " + ", ".join(missing) + "?"
                self._record_outgoing(message, "Asked for missing confirmation details")
                self.store.save(self.state)
                return message
            self.state.status = Status.CONFIRMED
            r = self.state.request
            message = f"Thank you very much. I confirm the booking: {r.restaurant_name}, {r.party_size} people, {human_date(r.booking_date)} at {human_time(r.booking_time)}."
            self._record_outgoing(message, "Repeated manager-confirmed booking details")
            self.store.save(self.state)
            return message
        return self._ask_client(incoming, "Manager response requires clarification")

    def _reply(self, message: str, decision: str, understood: str | None = None) -> str:
        self.state.status = Status.AWAITING_REPLY
        self._record_outgoing(message, decision, understood=understood)
        self.store.save(self.state)
        return message

    def answer_pending_question(self, client_answer: str) -> str:
        """Return client-supplied text verbatim; caller owns delivery and identity checks."""
        if not self.state.pending_question:
            raise ValueError("No question is awaiting a client answer")
        answer = client_answer.strip()
        if not answer or len(answer) > 2000:
            raise ValueError("Answer must contain 1 to 2000 characters")
        self.store.log("client_input", answer, "Client supplied an answer to the pending question", "Use supplied text without inventing details")
        self.state.messages.append({'role': 'client', 'content': answer})
        prefix = self.state.pending_reply_prefix
        outgoing = f'{prefix} {answer}' if prefix else answer
        self.state.pending_reply_prefix = None
        self.state.pending_question = None
        self.state.escalation_reason = None
        if prefix:
            return self._reply(outgoing, 'Сообщить выбранное допустимое время и передать ответ клиента без перевода.',
                               understood='Выбрано допустимое время; клиент предоставил недостающие сведения для продолжения брони.')
        return self._reply(answer, "Forward client-approved answer verbatim")

    def _ask_client(self, incoming: str, reason: str) -> None:
        reason = {
            "No verified answer available for manager's question": 'Собеседник задал вопрос, на который у меня нет ответа. Уточните, пожалуйста.',
            'Manager response requires clarification': 'Ответ собеседника требует уточнения. Как ему ответить?',
            'Booking name is unknown': 'На какое имя оформить бронь?',
            'Manager proposed a different or ambiguous time': 'Собеседник предложил другое время. Подходит ли оно вам?',
        }.get(reason, reason)
        self.state.status = Status.NEEDS_CLARIFICATION
        self.state.pending_question = incoming
        self.state.escalation_reason = reason
        self.store.log("decision", "", reason, "Waiting for client input; no facts invented")
        self.store.save(self.state)
        return None

    def _missing_confirmation_details(self, text: str) -> list[str]:
        r = self.state.request
        missing = []
        from .llm_policy import explicit_guests, explicit_times, negative_confirmation
        if explicit_guests(text) != {r.party_size}: missing.append(f'that it is for {r.party_size} people')
        if explicit_times(text) != {r.booking_time}: missing.append(f'the {r.booking_time} time')
        if negative_confirmation(text): missing.append('whether the booking is actually confirmed')
        if explicit_dates(text) != {r.booking_date}: missing.append(f"the date ({human_date(r.booking_date)})")
        return missing

    def _escalate(self, reason: str) -> None:
        self.state.status, self.state.escalation_reason = Status.ESCALATED, reason
        self.state.pending_question = None
        self.state.pending_reply_prefix = None
        self.state.pending_confirmation = None
        self.store.save(self.state)
        self.store.log("decision", "", "Safety trigger", f"Escalated: {reason}")
        return None

    def _record_outgoing(self, message: str, decision: str, understood: str | None = None) -> None:
        r = self.state.request
        incoming = self.store.input_record()
        if incoming and incoming['decision'] == 'Classifying reply safely':
            # Only fill an unfinished decision; never replace an already
            # interpreted manager message with an outgoing-message action.
            self.store.annotate(incoming['id'], decision='Использовать полученные сведения при подготовке ответа.')
        if understood:
            comprehension = understood
        elif self.state.status == Status.CONFIRMED:
            comprehension = (f'Все обязательные условия брони известны и проверены: {r.restaurant_name}, '
                             f'{r.party_size} гостей, {r.booking_date}, {r.booking_time}.')
        elif self.state.pending_confirmation:
            comprehension = 'Дата, время и число гостей подтверждены; для завершения брони не хватает только названия ресторана.'
        elif incoming and incoming['direction'] == 'client_input':
            comprehension = 'Клиент предоставил ответ на ожидающий вопрос; текст передаётся без изменений.'
        elif incoming:
            comprehension = 'Контекст заявки и переписки позволяет подготовить ответ от лица гостя.'
        else:
            comprehension = (f'Клиент запросил столик: {r.party_size} гостей, {r.booking_date}, '
                             f'{r.booking_time}; имя для брони: {r.booking_name or "не указано"}.')
        self.state.messages.append({"role": "agent", "content": message})
        self.store.log("outgoing", message, comprehension, decision)

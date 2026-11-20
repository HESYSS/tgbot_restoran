"""Local end-to-end rehearsal: no restaurant messages or network calls."""
from pathlib import Path
from uuid import uuid4
from .engine import BookingEngine
from .models import BookingRequest, ConversationState, Status
from .providers import RuleBasedProvider
from .store import StateStore


class LocalDialogue:
    def __init__(self, form: dict, directory: Path, provider=None):
        directory = directory / uuid4().hex
        request = BookingRequest(
            restaurant_name='Unknown restaurant', booking_date=form['booking_date'],
            booking_time=form['booking_time'], booking_name=form['booking_name'],
            party_size=form['party_size'], earliest=form['earliest'], latest=form['latest'],
        )
        self.engine = BookingEngine(ConversationState(request, status=Status.AWAITING_REPLY),
                                    provider or RuleBasedProvider(), StateStore(directory/'state.json', directory/'decisions.jsonl'))
        self.position = 0
        self.stopped = False

    async def next_question(self):
        if self.stopped:
            raise ValueError('Тест остановлен.')
        if self.engine.state.pending_question:
            raise ValueError('Сначала ответьте на вопрос менеджера.')
        r = self.engine.state.request
        script = ['What name should I book under?', 'How many guests?', 'Which day?',
                  'What time?', 'Do you have any allergies?',
                  f'This is Test Bistro. Confirmed for {r.party_size} people on {r.booking_date} at {r.booking_time}.']
        if self.position >= len(script):
            raise ValueError('Сценарий теста завершён.')
        question = script[self.position]
        self.position += 1
        reply = await self.engine.receive(question)
        if reply:
            self.engine.store.delivery(self.engine.store.last_outgoing_id, 'simulated')
        if self.engine.state.status in {Status.CONFIRMED, Status.CANCELLED, Status.ESCALATED}:
            self.stopped = True
        return question, reply

    def answer(self, text):
        reply = self.engine.answer_pending_question(text)
        self.engine.store.delivery(self.engine.store.last_outgoing_id, 'simulated')
        return reply

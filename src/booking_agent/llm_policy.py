"""Small final guard around a context-driven LLM conversation."""
import re
from datetime import date
import httpx
from .models import Status
from .display import explicit_dates, human_date, human_time, guests
from .restaurant import REMINDER, name_only_reply
from .offered_times import alternatives


def finish_declined(engine, reason, message='Thank you for letting me know. Have a lovely evening.'):
    reply = engine._reply(message, 'Поблагодарить собеседника и завершить диалог без подтверждения брони.',
                          understood='Забронировать столик на согласованных с клиентом условиях не удалось.')
    engine.state.pending_question = None
    engine.state.escalation_reason = None
    engine.state.pending_confirmation = None
    engine.state.status = Status.CANCELLED
    engine.store.save(engine.state)
    return reply


def accept_offer(engine, incoming, result, offered_times=None):
    r = engine.state.request
    offered_times = offered_times if offered_times is not None else alternatives(incoming, r.booking_time, is_offer=True)
    if not offered_times:
        clarification = 'Could you please write the available times in 24-hour format, for example 17:30?'
        if any(item['role'] == 'agent' and item['content'] == clarification for item in engine.state.messages):
            return engine._ask_client(incoming, 'Не удалось уточнить время у собеседника. Проверьте его ответ, чтобы не переспрашивать по кругу.')
        return engine._reply(clarification, 'Уточнить неоднозначное время у менеджера; не угадывать значение.')
    allowed = [time for time in offered_times if (r.earliest or r.booking_time) <= time <= (r.latest or r.booking_time)]
    engine.store.log('decision', '', 'Менеджер предложил варианты времени: ' + ', '.join(sorted(offered_times)) + '.',
                     'Сравнить все предложенные варианты с допустимым диапазоном клиента.')
    dates = explicit_dates(incoming)
    party = explicit_guests(incoming)
    if (dates and dates != {r.booking_date}) or (party and party != {r.party_size}):
        return finish_declined(engine, 'Alternative date or party size does not match request',
            'Thank you, but that would not work for our booking. Have a lovely evening.')
    if not allowed:
        if r.ask_before_alternative:
            engine.state.pending_time_offer = dict(times=sorted(offered_times), incoming=incoming)
            engine.state.pending_reply_prefix = None
            engine.state.pending_confirmation = None
            return engine._ask_client(incoming, f'На {r.booking_time} мест нет. Предлагают ' + ', '.join(sorted(offered_times))
                + ' (Лондон). Какое время вам подходит? Введите один из вариантов в формате ЧЧ:ММ или «Нет», чтобы отказаться. До вашего решения ничего не согласую.')
        return finish_declined(engine, 'Alternative outside client-approved time window',
            'Thank you for the offer, but that time would not work for us. Have a lovely evening.')
    def minutes(time):
        hour, minute = map(int, time.split(':'))
        return hour * 60 + minute
    proposed = min(allowed, key=lambda time: (abs(minutes(time)-minutes(r.booking_time)), time))
    r.booking_time = proposed
    restaurant = result.get('restaurant')
    if isinstance(restaurant, str) and restaurant.strip() and restaurant.casefold() in incoming.casefold():
        r.restaurant_name = restaurant.strip()
    engine.state.pending_question = None
    engine.state.escalation_reason = None
    question = result.get('client_question')
    if not question and result['action'] == 'ask_client':
        question = result['message']
    preference = re.search(r'\b(?:would|do)\s+you\s+(?:prefer|rather(?: have)?)\s+', incoming, re.I)
    if preference:
        # A preference among already-approved alternatives needs no client input.
        # Preserve a separate question if one follows the preference question.
        remainder = incoming[preference.end():]
        preference_clause = re.split(r'[?.!]', remainder, maxsplit=1)
        rest = preference_clause[1].strip() if len(preference_clause) > 1 else ''
        if not rest:
            question = None
    if not question and re.search(r'\b(?:contact number|phone number|telephone number)\b', incoming, re.I):
        question = 'Какой контактный номер можно передать ресторану?'
    if question:
        engine.state.pending_reply_prefix = f'{human_time(proposed).capitalize()} would be lovely, thank you.'
        return engine._ask_client(incoming, f'Подходит время {proposed}. {question}')
    return engine._reply(f'{human_time(proposed).capitalize()} would be lovely, thank you. '
        f'Could you please confirm the booking for {guests(r.party_size)} on {human_date(r.booking_date)}?',
        'Accepted manager offer within client-approved time window; requested explicit confirmation')


def negative_confirmation(text):
    return bool(re.search(r"\b(not|no|never|cannot|can't|unable|unconfirmed|pending|tentative|provisional)\b", text, re.I))


def explicit_guests(text):
    words = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
             'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
    found = set()
    for match in re.finditer(r'\b(?:for\s+)?(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:people|persons|guests|diners)\b', text, re.I):
        value = match.group(1).lower()
        found.add(int(value) if value.isdigit() else words[value])
    for match in re.finditer(r'\b(?:table|party)\s+(?:for|of)\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b', text, re.I):
        value = match.group(1).lower()
        found.add(int(value) if value.isdigit() else words[value])
    return found


def explicit_times(text):
    times = set()
    for match in re.finditer(r'\b([01]?\d|2[0-3]):([0-5]\d)\s*(am|pm)?\b', text, re.I):
        hour, minute, period = int(match[1]), int(match[2]), match[3]
        if period:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if period.lower() == 'pm' else 0)
        times.add(f'{hour:02d}:{minute:02d}')
    for match in re.finditer(r'\b(1[0-2]|[1-9])\s*(am|pm)\b', text, re.I):
        hour = int(match[1]) % 12 + (12 if match[2].lower() == 'pm' else 0)
        times.add(f'{hour:02d}:00')
    return times


async def receive_with_llm(engine, incoming):
    try:
        result = await engine.provider.decide(engine.state.request, engine.state.messages)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, RuntimeError):
        return engine._escalate('Groq недоступен, исчерпан лимит или ответ не прошёл проверку. Диалог приостановлен без отправки; проверьте журнал перед продолжением.')
    engine.store.log('decision', '', result['reason'], result['action'])
    r = engine.state.request
    supplied_name = result.get('restaurant')
    manager_texts = [item['content'].casefold() for item in engine.state.messages if item['role'] == 'manager']
    if isinstance(supplied_name, str) and supplied_name.strip() and any(supplied_name.casefold() in text for text in manager_texts):
        r.restaurant_name = supplied_name.strip()
        engine.store.save(engine.state)
    evidence = incoming
    pending = engine.state.pending_confirmation
    if pending and r.restaurant_name != 'Unknown restaurant' and name_only_reply(incoming, r.restaurant_name):
        # Only the missing name is supplied now. Other facts were explicitly
        # validated in the earlier manager confirmation and survive a restart.
        evidence = pending['evidence']
        result = dict(result, action='confirm', restaurant=r.restaurant_name,
                      date=pending['date'], time=pending['time'], guests=pending['guests'])
        engine.store.log('decision', '', f'Менеджер сообщил недостающее название ресторана: {r.restaurant_name}.',
                         'Сохранить название и завершить проверку ранее подтверждённых условий.')
    elif pending:
        engine.state.pending_confirmation = None
        engine.store.save(engine.state)
    action = result['action']
    # Correct the common misrouting observed during the live rehearsal, even if
    # the model asks the client to repeat an already supplied time preference.
    positive_offer = bool(re.search(r'\b(?:we (?:can|could) offer|we (?:have|could do)|would .+ suit|available instead)\b', incoming, re.I))
    if '?' in incoming and not re.search(r'\b(?:would that|does that|would .+ suit)\b', incoming, re.I):
        positive_offer = False
    times = explicit_times(incoming)
    offered = alternatives(incoming, r.booking_time, is_offer=action == 'offer_time')
    if action != 'confirm' and offered:
        return accept_offer(engine, incoming, result, offered)
    if action == 'offer_time':
        return accept_offer(engine, incoming, result)
    refusal_without_offer = bool(re.search(r"(?:don[’']t have|do not have|not available|no tables? available|no availability|cannot accommodate|fully booked at)", incoming, re.I))
    final_refusal = bool(re.search(r'rest of the evening|(?:fully booked|no tables?).*(?:all evening|for the evening|rest of|entire|whole)', incoming, re.I))
    if final_refusal and not positive_offer and action != 'confirm':
        return finish_declined(engine, 'Explicit final refusal for the evening')
    if action == 'unavailable' or (action in {'reply', 'ask_client', 'declined'} and refusal_without_offer and not positive_offer and not final_refusal):
        engine.state.pending_question = None
        engine.state.escalation_reason = None
        return engine._reply('Thank you for letting me know. What times do you have available?',
                             'Requested alternatives from manager; client time window already known')
    if action == 'ask_client':
        return engine._ask_client(incoming, result['message'] or 'Нужны дополнительные сведения клиента.')
    if action == 'ask_manager':
        question = result['message'].strip()
        if not question or re.search(r'\b(confirmed|booked|reserved|I confirm|we confirm)\b', question, re.I):
            question = 'Could you please clarify the details of your last message?'
        engine.state.pending_question = None
        engine.state.escalation_reason = None
        return engine._reply(question, 'Уточнить недостающие или противоречивые сведения у менеджера; не угадывать.')
    if action == 'declined':
        return finish_declined(engine, result['reason'])
    if action == 'confirm':
        r = engine.state.request
        try:
            proposed = result['time']
            valid = (type(result['guests']) is int and result['guests'] == r.party_size
                     and date.fromisoformat(result['date']).isoformat() == r.booking_date
                     and explicit_times(evidence) == {proposed}
                     and (r.earliest or r.booking_time) <= proposed <= (r.latest or r.booking_time)
                     and explicit_guests(evidence) == {r.party_size}
                     and explicit_dates(evidence) == {r.booking_date}
                     and not negative_confirmation(evidence))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return engine._reply('Thank you. Could you please confirm '
                f'{r.party_size} guests, {human_date(r.booking_date)} at {human_time(r.booking_time)}?',
                'Confirmation rejected: missing or conflicting booking details')
        if r.restaurant_name == 'Unknown restaurant':
            engine.state.pending_confirmation = dict(evidence=evidence, date=r.booking_date, time=proposed, guests=r.party_size)
            engine.store.log('decision', '', f'Менеджер подтвердил {r.party_size} гостей, {r.booking_date}, {proposed}; название ресторана неизвестно.',
                             'Сохранить подтверждённые условия и уточнить только название ресторана.')
            return engine._reply(REMINDER, 'Тактично уточнить недостающее название ресторана; не переспрашивать остальные условия.')
        r.booking_time = proposed
        engine.state.pending_confirmation = None
        message = f'Thank you very much. I confirm the booking for {guests(r.party_size)} at {r.restaurant_name} on {human_date(r.booking_date)} at {human_time(r.booking_time)}.'
        engine.state.pending_question = None
        engine.state.escalation_reason = None
        engine.state.status = Status.CONFIRMED
        if evidence == incoming:
            engine.store.log('decision', '', f'Менеджер подтвердил бронь: {r.restaurant_name}, {r.party_size} гостей, {r.booking_date}, {r.booking_time}.',
                             'Условия совпадают с заявкой; подготовить итоговое подтверждение.')
        final_decision = 'Отправить итоговое подтверждение и завершить диалог после успешной доставки.'
        engine._record_outgoing(message, final_decision)
        engine.store.save(engine.state)
        return message
    reply = result['message'].strip()
    if re.search(r'\b(?:we (?:can|could) offer|we have (?:a table|availability)|would you like to (?:book|confirm))\b', reply, re.I):
        return engine._reply('Thank you. Could you please clarify the available time for our booking?',
                             'Restaurant-role wording rejected; requested clarification as guest')
    if not reply or re.search(r'\b(confirmed|booked|reserved|reservation is set|I confirm|we confirm)\b', reply, re.I):
        return engine._ask_client(incoming, 'Ответ модели похож на подтверждение без проверки условий. Проверьте ответ собеседника.')
    engine.state.pending_question = None
    engine.state.escalation_reason = None
    return engine._reply(reply, result['reason'])

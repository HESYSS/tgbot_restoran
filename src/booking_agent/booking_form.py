"""Validated booking form; no network or Telegram dependencies."""
from datetime import date, timedelta
import re

EXACT = "🎯 Ровно в это время"
FLEX = "↔️ Можно раньше или позже"
BEFORE = "⏰ Не позже"
RANGE = "🕒 Указать диапазон"
CONFIRM = "🧪 Проверить диалог локально"
LIVE_CONFIRM = "✍️ Написать текстом"
VOICE_CONFIRM = "🎙 Отправить голосовое"
RESTART = "✏️ Заполнить заново"


def clock(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,2}:[0-9]{2}", value):
        raise ValueError("Введите время в формате ЧЧ:ММ, например 17:00.")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("Время должно быть от 00:00 до 23:59.")
    return hour * 60 + minute


def format_clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def advance(form: dict, value: str, today: date) -> None:
    """Mutate only after valid input; callers persist after successful advance."""
    step = form.get("step", "name")
    if step == "name":
        if len(value.split()) < 2 or len(value) > 120:
            raise ValueError("Введите имя и фамилию, на кого оформить бронь.")
        form.update(booking_name=value, step="guests")
    elif step == "guests":
        if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= 30:
            raise ValueError("Нужно целое число гостей от 1 до 30, например 2. Без слов, дробей и знаков. Для группы больше 30 гостей нужна отдельная договорённость с рестораном.")
        form.update(party_size=int(value), step="date")
    elif step == "date":
        try:
            selected = today if value == "Сегодня" else today + timedelta(days=1) if value == "Завтра" else date.fromisoformat(value)
        except ValueError:
            raise ValueError("Выберите сегодня, завтра или введите дату ГГГГ-ММ-ДД.") from None
        if selected < today:
            raise ValueError("Выберите сегодняшнюю или будущую дату.")
        form.update(booking_date=selected.isoformat(), step="time")
    elif step == "time":
        if len(re.findall(r"[0-9]{1,2}:[0-9]{2}", value)) == 2:
            raise ValueError("Сейчас нужно одно желаемое время, например 17:00. Диапазон можно указать на следующем шаге через «Указать диапазон».")
        form.update(booking_time=format_clock(clock(value)), step="flexibility")
    elif step == "flexibility":
        minutes = clock(form["booking_time"])
        if value == EXACT:
            form.update(earliest=form["booking_time"], latest=form["booking_time"], step="restaurant")
        elif value == FLEX:
            form["step"] = "tolerance"
        elif value == BEFORE:
            form.update(latest=form["booking_time"], step="earliest")
        elif value == RANGE:
            form["step"] = "range"
        else:
            raise ValueError("Выберите условие кнопкой ниже. Для интервала вроде «от 16:00 до 22:59» нажмите «🕒 Указать диапазон».")
    elif step == "tolerance":
        if not re.fullmatch(r"[0-9]{1,3}", value) or not 1 <= int(value) <= 720:
            raise ValueError("Введите допустимое отклонение целым числом минут от 1 до 720, например 30.")
        minutes, delta = clock(form['booking_time']), int(value)
        if minutes - delta < 0 or minutes + delta >= 1440:
            raise ValueError("Такой интервал пересекает полночь. Введите меньше минут или отмените заявку и выберите диапазон в пределах одной даты.")
        form.update(earliest=format_clock(minutes-delta), latest=format_clock(minutes+delta), step="restaurant")
    elif step == "earliest":
        if clock(value) > clock(form["latest"]):
            raise ValueError("Самое раннее время должно быть не позже предельного.")
        form.update(earliest=format_clock(clock(value)), step="restaurant")
    elif step == "range":
        match = re.fullmatch(r"\s*(?:от\s+)?([0-9]{1,2}:[0-9]{2})\s*(?:[-–—]|до)\s*([0-9]{1,2}:[0-9]{2})\s*", value, re.I)
        if not match:
            raise ValueError("Нужны два времени. Подойдёт «16:00-22:59» или «от 16:00 до 22:59».")
        start, end = (format_clock(clock(part)) for part in match.groups())
        if not clock(start) <= clock(form["booking_time"]) <= clock(end):
            raise ValueError(f"Начало должно быть не позже конца, а желаемое время {form['booking_time']} — внутри диапазона. Например, для 17:00 подходит 16:00-22:59. Переход через полночь пока не поддерживается.")
        form.update(earliest=start, latest=end, step="restaurant")
    elif step == "restaurant":
        if not value or len(value) > 160:
            raise ValueError("Введите название ресторана или «Не знаю».")
        form.update(restaurant_name="Unknown restaurant" if value == "Не знаю" else value, step="manager")
    elif step == "manager":
        if value.lower() not in {'@project_sync_bot', '@vlad_dev_7'}:
            raise ValueError("Введите @username тестового собеседника: @project_sync_bot или @vlad_dev_7.")
        form.update(manager=value.lower(), recipient_entered_manually=True, step="review")


def prompt(form: dict) -> tuple[str, list[str]]:
    step = form.get("step", "name")
    questions = {
        "name": ("1/7 · На кого бронируем?\nВведите имя и фамилию.", []),
        "guests": ("2/7 · Сколько гостей?\nВведите целое число от 1 до 30, например 2.", []),
        "date": ("3/7 · На какую дату?\nДля другой даты: ГГГГ-ММ-ДД.", ["Сегодня", "Завтра"]),
        "time": ("4/7 · Во сколько вам удобнее?\nВведите одно время, например 17:00. На следующем шаге можно задать гибкость. Всё время — по Лондону.", []),
        "flexibility": (f"Желаемое время: {form.get('booking_time', '')}.\nЕсли оно занято, какие варианты вам подойдут?", [EXACT, FLEX, BEFORE, RANGE]),
        "tolerance": ("На сколько минут можно раньше или позже?\nВведите число минут, например 30. Для 17:00 это будет 16:30–17:30.", []),
        "earliest": ("С какого времени вам удобно?\nВведите ЧЧ:ММ. Агент не согласится на более раннее время.", []),
        "range": ("Введите два времени в пределах выбранной даты.\nНапример: 16:00-22:59 или от 16:00 до 22:59.\nЖелаемое время должно попадать в этот диапазон.", []),
        "restaurant": ("5/7 · Как называется ресторан?\nЕсли не знаете, агент уточнит у менеджера.", ["Не знаю"]),
        "manager": ("Кому отправить заявку?\nВведите @username собеседника вручную. Отправки на этом шаге нет.", []),
    }
    if step != "review":
        return questions[step]
    venue = form['restaurant_name'] if form['restaurant_name'] != 'Unknown restaurant' else 'уточним у менеджера'
    return (f"7/7 · Проверьте заявку\n\nНа имя: {form['booking_name']}\nГостей: {form['party_size']}\n"
            f"Дата: {form['booking_date']}\nГород: Лондон\nЖелаемое время: {form['booking_time']}\n"
            f"Допустимо: {form['earliest']}–{form['latest']} (Лондон)\nРесторан: {venue}\n"
            f"Адресат: {form['manager']}\nОтправитель: {'ваш аккаунт' if form['sender_mode'] == 'personal' else 'представитель сервиса'}\n\n"
            "Как отправить заявку? Выбранный формат применяется к первому сообщению и дальнейшим ответам агента. "
            "Нажатие отправит имя и детали указанному выше адресату. При сбое озвучивания ответ уйдёт текстом. "
            "«Проверить локально» оставляет переписку в имитации.", [LIVE_CONFIRM, VOICE_CONFIRM, CONFIRM, RESTART])

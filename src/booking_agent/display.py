"""Locale-independent English date formatting and explicit date recognition."""
from datetime import date
import re

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']


def guests(count):
    return {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six'}.get(count, str(count))


def human_date(iso):
    day = date.fromisoformat(iso)
    return f'{day.day} {MONTHS[day.month-1]} {day.year}'


def human_time(value):
    hour, minute = map(int, value.split(':'))
    return f'{hour % 12 or 12}' + (f':{minute:02d}' if minute else '') + (' pm' if hour >= 12 else ' am')


def explicit_dates(text):
    found = set()
    for match in re.finditer(r'\b\d{4}-\d{2}-\d{2}\b', text):
        try:
            found.add(date.fromisoformat(match[0]).isoformat())
        except ValueError:
            pass
    names = '|'.join(MONTHS + [month[:3] for month in MONTHS])
    patterns = [rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({names})\.?\s*,?\s*(\d{{4}})\b',
                rf'\b({names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b']
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text, re.I):
            day, month, year = match.groups() if index == 0 else (match[2], match[1], match[3])
            try:
                number = next(i+1 for i, name in enumerate(MONTHS) if name.lower().startswith(month.lower()))
                found.add(date(int(year), number, int(day)).isoformat())
            except (ValueError, StopIteration):
                pass
    return found

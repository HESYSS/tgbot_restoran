"""Evidence-backed alternatives, including British 'half five or six'."""
import re

WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
         'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12}
HOUR = r'(?:[01]?\d|2[0-3]|' + '|'.join(WORDS) + ')'
TIME = rf'(?:half\s+{HOUR}|quarter\s+(?:past|to)\s+{HOUR}|{HOUR}(?::[0-5]\d)?)(?:\s*(?:am|pm))?'
TOKEN = re.compile(rf'(?<!\w)({TIME})(?!\w|:)', re.I)


def alternatives(text, reference_time, *, is_offer=False):
    """Do not include a rejected time appearing before the offer clause."""
    marker = re.search(r'\b(?:i|we)\s+(?:(?:can|could)\s+)?(?:offer|do)\b', text, re.I)
    preference = re.search(r'\b(?:would|do)\s+you\s+(?:prefer|rather(?: have)?)\s+(.+)', text, re.I)
    if marker:
        section = text[marker.end():].split('.')[0].split('?')[0]
        # A party size is not a time; only parse after 'at', unless the offer
        # starts directly with a time ('I could do half five or six').
        at = re.search(r'\bat\s+', section, re.I)
        if at:
            section = section[at.end():]
        elif not re.match(rf'\s*{TIME}\b', section, re.I):
            return set()
    elif preference:
        section = preference[1].split('?')[0].split('.')[0]
    elif re.fullmatch(rf'\s*{TIME}(?:\s*(?:or|and|,)\s*{TIME})*\s*[.?]?\s*', text, re.I):
        section = text
    elif is_offer:
        at = re.search(r'\b(?:at|would)\s+(.+)', text, re.I)
        if not at:
            return set()
        section = at[1].split('?')[0].split('.')[0]
    else:
        return set()
    # Remove date, phone, and other following clauses before extracting times.
    section = re.split(r'\b(?:on|today|tomorrow|tonight|under|and could|and can|contact|phone)\b', section, flags=re.I)[0]
    evening = bool(re.search(r'\b(?:tonight|evening|dinner|pm)\b', text, re.I))
    afternoon = evening or int(reference_time.split(':')[0]) >= 12
    if re.search(r'\bin the morning\b', text, re.I) and not evening:
        afternoon = False
    found = set()
    for match in TOKEN.finditer(section):
        value = match[1].lower().strip()
        period = re.search(r'\s*(am|pm)$', value)
        if period:
            value = value[:period.start()].strip()
        minute = 0
        if value.startswith('half '):
            value, minute = value[5:], 30
        elif value.startswith('quarter past '):
            value, minute = value[13:], 15
        elif value.startswith('quarter to '):
            value, minute = value[11:], -15
        if ':' in value:
            hour, minute = map(int, value.split(':'))
        else:
            hour = WORDS[value] if value in WORDS else int(value)
        if period:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if period[1] == 'pm' else 0)
        elif ':' not in value and 1 <= hour < 12 and afternoon:
            hour += 12
        total = hour * 60 + minute
        if 0 <= total < 1440:
            found.add(f'{total // 60:02d}:{total % 60:02d}')
    return found

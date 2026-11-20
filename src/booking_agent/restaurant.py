"""Remember an explicit restaurant introduction, never agent/client guesses."""
import re

REMINDER = "Could you remind me of the restaurant's name for my booking notes, please?"


def introduced_name(text):
    match = re.search(
        r"\b(?:this is|welcome to|(?:the )?restaurant(?:'s)? name is|(?:the )?restaurant is called)\s+([^.,!?\n]{2,80})(?=[.,!?\n]|$)",
        text, re.I,
    )
    if not match:
        return None
    candidate = match[1].strip()
    if re.search(r'\b(?:confirmed|available|unfortunately|fully|sorry|fine)\b', candidate, re.I):
        return None
    return candidate


def name_only_reply(text, name):
    prefix = r"(?:yes[, ]+)?(?:(?:this is|welcome to|the restaurant is called|the restaurant name is|it[’']s)\s+)?"
    return bool(re.fullmatch(prefix + re.escape(name) + r'[.!\s]*', text.strip(), re.I))

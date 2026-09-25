"""Recognising which script/language a customer writes in - in code, no AI.

Used by the Arabizi guard: once we know a customer writes Arabizi, replies
to them must contain only Latin letters and numbers.
"""

from __future__ import annotations

import re

from cozysetup.database import Language

# Arabic script, including the extra blocks and presentation forms.
ARABIC_SCRIPT = re.compile("[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

# In Arabizi, digits stand for Arabic letters inside words: 3 = ع, 7 = ح, 2 = ء, 5 = خ, 6 = ط, 8 = ق, 9 = ص.
_WORD = re.compile(r"[a-z0-9']+")
_ARABIZI_DIGIT = re.compile(r"[2356789]")
# Ordinary English words with digits in them - not Arabizi.
_NOT_ARABIZI = re.compile(r"\d+(st|nd|rd|th|am|pm|kwd|kd|k|m|h|min|mins|hrs?)")
# Unmistakable Arabizi words without digits.
_ARABIZI_WORDS = {"shlonkum", "shlonk", "shlonik", "shlonich", "shnu", "shno", "wayed", "abgha"}


def has_arabic_script(text: str) -> bool:
    return bool(ARABIC_SCRIPT.search(text))


def detect_language(text: str) -> Language | None:
    """ar for Arabic script, arabizi for clear Arabizi, None when it can't tell
    (English, or short things like "ok" or a phone number)."""
    if has_arabic_script(text):
        return Language.ARABIC
    for word in _WORD.findall(text.lower()):
        if word in _ARABIZI_WORDS:
            return Language.ARABIZI
        has_letter = any(ch.isalpha() for ch in word)
        if has_letter and _ARABIZI_DIGIT.search(word) and not _NOT_ARABIZI.fullmatch(word):
            return Language.ARABIZI
    return None

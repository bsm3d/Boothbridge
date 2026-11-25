# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Interface language handling: detects the system language and switches the translated texts.

from __future__ import annotations

import locale
import sys

# code and the name of the language written in that language
LANGUAGES = {
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "zh": "中文",
}
SOURCE = "en"

_lang = SOURCE
_words: dict[str, str] = {}

# ------------------------------------------------------------
# the language of the computer when the program knows it, else English
def system_language() -> str:
    code = ""
    try:
        if sys.platform == "win32":
            import ctypes

            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                code = buf.value
        if not code:
            code = locale.getlocale()[0] or ""
    except (OSError, ValueError, AttributeError):
        pass
    code = code.replace("_", "-").split("-")[0].lower()
    return code if code in LANGUAGES else SOURCE

# the words of the language, from every part of the lang package
def set_language(code: str):
    global _lang, _words
    from . import lang

    if code not in LANGUAGES:
        code = SOURCE
    _lang = code
    _words = {}
    for part in lang.PARTS:
        _words.update(part.CATALOG.get(code, {}))

def language() -> str:
    return _lang

# ------------------------------------------------------------
# the text of the interface (tr), the name is not t on purpose, a track is t in most of the code: the English text is the key, a missing translation shows the English
# one. The values go in as {name} fields, the order of the words changes from a language to another
def tr(text: str, **fields) -> str:
    out = _words.get(text, text)
    if not fields:
        return out
    try:
        return out.format(**fields)
    except (KeyError, IndexError, ValueError):
        return text.format(**fields)

# one or many, each with its own text
def trn(n: int, one: str, many: str, **fields) -> str:
    return tr(one if n == 1 else many, n=n, **fields)

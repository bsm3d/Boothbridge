# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Package that gathers the translation parts of the interface and lists them in one tuple.

#
# A part holds CATALOG = {"fr": {english text: translation}, "de": ..., "es": ..., "zh": ...}.
# English is the text in the code, it has no catalog. tools/i18n_check.py lists what is missing.

from . import core, ui_details, ui_dialogs, ui_edit, ui_library, ui_main

PARTS = (core, ui_details, ui_dialogs, ui_edit, ui_library, ui_main)

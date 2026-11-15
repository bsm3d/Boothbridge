# Benoit Saint-Moulin
# Boothbridge : the translations of the interface, one part per area of the program
#
# A part holds CATALOG = {"fr": {english text: translation}, "de": ..., "es": ..., "zh": ...}.
# English is the text in the code, it has no catalog. tools/i18n_check.py lists what is missing.

from . import core, ui_details, ui_dialogs, ui_edit, ui_library, ui_main

PARTS = (core, ui_details, ui_dialogs, ui_edit, ui_library, ui_main)

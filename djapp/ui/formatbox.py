# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Helpers that fill and read the export format combo box, keeping each format's internal name as data.

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from .. import settings

# the label has the beta mention, the data of the item is the name the export knows
def fill(box: QComboBox, formats, current: str = ""):
    box.clear()
    for fmt in formats:
        box.addItem(settings.export_label(fmt), fmt)
    select(box, current if current in formats else (formats[0] if formats else ""))

def name(box: QComboBox) -> str:
    return box.currentData() or box.currentText()

def select(box: QComboBox, fmt: str):
    i = box.findData(fmt)
    if i >= 0:
        box.setCurrentIndex(i)

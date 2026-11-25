# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : First-start language choice dialog, listing every language in its own words.

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from .. import i18n

# ------------------------------------------------------------
class LanguageDialog(QDialog):
    def __init__(self, current: str = "", parent=None):
        super().__init__(parent)
        # every language in its own words, nobody has chosen yet
        self.setWindowTitle("Language / Langue / Sprache / Idioma / 语言")
        self.setMinimumWidth(340)
        self.choice = current if current in i18n.LANGUAGES else i18n.SOURCE
        self.list = QListWidget()
        for code, name in i18n.LANGUAGES.items():
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, code)
            self.list.addItem(item)
            if code == self.choice:
                self.list.setCurrentItem(item)
        self.list.itemDoubleClicked.connect(lambda _=None, *_rest: self.accept())
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Language / Langue / Sprache / Idioma / 语言"))
        lay.addWidget(self.list)
        lay.addWidget(ok, 0, Qt.AlignRight)

    def accept(self):
        item = self.list.currentItem()
        if item is not None:
            self.choice = item.data(Qt.UserRole)
        super().accept()

# the code of the language, asked once
def choose(current: str = "") -> str:
    dlg = LanguageDialog(current or i18n.system_language())
    dlg.exec()
    return dlg.choice

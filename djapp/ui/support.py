# Benoit Saint-Moulin
# Boothbridge : the link that supports the project, shown once and always in the Help menu

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from ..brand import APP_NAME, SUPPORT_URL
from ..i18n import tr


# the browser opens only when the user clicks, the program never reaches the network by itself
def open_link(*_):
    if SUPPORT_URL:
        QDesktopServices.openUrl(QUrl(SUPPORT_URL))


# once, after the first export that went well: a thank you with a way to support, never again after an answer
def ask_once(parent, cfg: dict) -> bool:
    if not SUPPORT_URL or cfg.get("support_asked"):
        return False
    cfg["support_asked"] = True
    box = QMessageBox(parent)
    box.setWindowTitle(tr("{app} is free", app=APP_NAME))
    box.setText(
        tr(
            "{app} is free and made by one person in his free time. If it saves you time, "
            "you can support it. Nothing is asked in return and this message does not come back.",
            app=APP_NAME,
        )
    )
    yes = box.addButton(tr("Support the project"), QMessageBox.AcceptRole)
    box.addButton(tr("Not now"), QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is yes:
        open_link()
    return True

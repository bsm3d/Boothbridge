# Boothbridge
# © Benoit Saint-Moulin - 2026
# Version du fichier : 1.0.0
# Fonction du fichier : Qt widget that draws the memory curve per cycle, with the warm-up shaded and the fitted line on top.

from __future__ import annotations

from djapp.ui import theme
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .memory import fit

SERIES = {"private": ("Mémoire privée", 1048576.0, "Mo"), "rss": ("Mémoire résidente", 1048576.0, "Mo"),
          "heap": ("Tas Python (tracemalloc)", 1048576.0, "Mo"), "widgets": ("Widgets vivants", 1.0, ""),
          "qobjects": ("QObject en mémoire", 1.0, ""), "threads": ("Threads Python", 1.0, ""),
          "handles": ("Poignées ouvertes", 1.0, ""), "children": ("Processus enfants", 1.0, "")}


# ------------------------------------------------------
class Curve(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []
        self.warmup = 0
        self.key = "private"
        self.setMinimumHeight(220)

    def set_data(self, rows: list[dict], warmup: int):
        self.rows, self.warmup = rows, warmup
        self.update()


    def set_key(self, key: str):
        self.key = key
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.BG_MED))
        label, unit, suffix = SERIES[self.key]
        area = QRectF(70, 14, max(10, self.width() - 90), max(10, self.height() - 44))
        p.setPen(QColor(theme.FG_MUTED))
        if len(self.rows) < 2:
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Aucune mesure pour le moment.")
            return
        ys = [r[self.key] / unit for r in self.rows]
        low, high = min(ys), max(ys)
        if high - low < 1e-9:
            low, high = low - 1, high + 1
        pad = (high - low) * 0.08
        low, high = low - pad, high + pad
        n = len(ys)

        def point(i, y):
            return QPointF(area.left() + area.width() * i / (n - 1), area.bottom() - area.height() * (y - low) / (high - low))

        p.setPen(QPen(QColor(theme.BG_LIGHT), 1))
        for k in range(5):
            y = low + (high - low) * k / 4
            at = point(0, y).y()
            p.drawLine(QPointF(area.left(), at), QPointF(area.right(), at))
            p.setPen(QColor(theme.FG_MUTED))
            p.drawText(QRectF(0, at - 9, 64, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{y:.1f}" if unit > 1 else f"{y:.0f}")
            p.setPen(QPen(QColor(theme.BG_LIGHT), 1))
        if self.warmup > 0:
            end = point(min(self.warmup, n - 1), 0).x()
            p.fillRect(QRectF(area.left(), area.top(), end - area.left(), area.height()), QColor(255, 255, 255, 22))
            p.setPen(QColor(theme.FG_MUTED))
            p.drawText(QRectF(area.left() + 4, area.top(), 110, 16), Qt.AlignmentFlag.AlignLeft, "échauffement")
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        for i in range(n - 1):
            p.drawLine(point(i, ys[i]), point(i + 1, ys[i + 1]))
        p.setBrush(QColor(theme.ACCENT))
        for i, y in enumerate(ys):
            p.drawEllipse(point(i, y), 2.5, 2.5)
        kept = ys[self.warmup:]
        if len(kept) >= 2:
            slope, r2 = fit(kept)
            mean_x = (len(kept) - 1) / 2
            mean_y = sum(kept) / len(kept)
            start = self.warmup
            a = point(start, mean_y - slope * mean_x)
            b = point(n - 1, mean_y + slope * (len(kept) - 1 - mean_x))
            p.setPen(QPen(QColor(theme.YELLOW), 2, Qt.PenStyle.DashLine))
            p.drawLine(a, b)
            p.setPen(QColor(theme.YELLOW))
            p.drawText(QRectF(area.left(), self.height() - 22, area.width(), 18), Qt.AlignmentFlag.AlignLeft,
                       f"{label} : pente {slope:+.2f} {suffix}/cycle, R2 {r2:.2f}")
        p.setPen(QColor(theme.FG_MUTED))
        p.drawText(QRectF(area.right() - 120, self.height() - 22, 120, 18), Qt.AlignmentFlag.AlignRight, f"{n} cycles")

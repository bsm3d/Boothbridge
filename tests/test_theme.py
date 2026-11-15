import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from djapp.ui import theme
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollBar, QStyle, QStyleOptionSlider


# ------------------------------------------------
class ScrollbarThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_scrollbar_chevrons_are_valid_packaged_assets(self):
        for name in ("scroll-left.svg", "scroll-right.svg", "spin-up.svg", "spin-down.svg"):
            path = Path(theme.ICONS) / name
            self.assertTrue(path.is_file(), path)
            self.assertTrue(QSvgRenderer(str(path)).isValid(), path)
            self.assertIn(f"{theme.ICONS}/{name}", theme.STYLE)

    def test_both_scrollbar_orientations_render_blue_and_keep_native_input(self):
        for orientation in (Qt.Horizontal, Qt.Vertical):
            with self.subTest(orientation=orientation):
                bar = QScrollBar(orientation)
                try:
                    bar.setStyleSheet(theme.STYLE)
                    bar.setRange(0, 100)
                    bar.setPageStep(20)
                    bar.setSingleStep(1)
                    bar.setValue(50)
                    bar.resize(300, 14) if orientation == Qt.Horizontal else bar.resize(14, 300)
                    bar.show()
                    QTest.mouseMove(bar, QPoint(-10, -10))
                    self.app.processEvents()
                    option = QStyleOptionSlider()
                    bar.initStyleOption(option)
                    handle = bar.style().subControlRect(
                        QStyle.CC_ScrollBar, option, QStyle.SC_ScrollBarSlider, bar,
                    )
                    image = bar.grab().toImage()
                    scale = image.devicePixelRatio()
                    centre = handle.center()
                    self.assertEqual(
                        image.pixelColor(round(centre.x() * scale), round(centre.y() * scale)),
                        QColor(theme.ACCENT),
                    )
                    self.assertEqual(bar.height() if orientation == Qt.Horizontal else bar.width(), 14)
                    for control, expected in ((QStyle.SC_ScrollBarAddLine, 51),
                                              (QStyle.SC_ScrollBarSubLine, 50)):
                        rect = bar.style().subControlRect(QStyle.CC_ScrollBar, option, control, bar)
                        QTest.mouseClick(bar, Qt.LeftButton, Qt.NoModifier, rect.center())
                        self.assertEqual(bar.value(), expected)
                    QTest.keyClick(bar, Qt.Key_End)
                    self.assertEqual(bar.value(), 100)
                    QTest.keyClick(bar, Qt.Key_Home)
                    self.assertEqual(bar.value(), 0)
                finally:
                    bar.close()


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------
class SelectionThemeTests(unittest.TestCase):
    @staticmethod
    def luminance(color: str) -> float:
        rgb = [c / 255 for c in QColor(color).getRgb()[:3]]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def contrast(self, a: str, b: str) -> float:
        hi, lo = sorted((self.luminance(a), self.luminance(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def test_a_selected_row_is_a_tint_with_light_text_not_the_accent(self):
        self.assertIn(f"selection-background-color: {theme.SELECTION}", theme.STYLE)
        self.assertIn(f"selection-color: {theme.FG}", theme.STYLE)
        self.assertNotEqual(theme.SELECTION, theme.ACCENT)
        self.assertGreaterEqual(self.contrast(theme.FG, theme.SELECTION), 7)

    def test_the_selected_row_still_stands_out_from_the_list(self):
        self.assertGreaterEqual(self.contrast(theme.SELECTION, theme.BG_MED), 1.2)

    def test_the_metronome_icon_is_drawn_for_every_state(self):
        from djapp.ui.metronome import metronome_icon
        from PySide6.QtGui import QIcon

        QApplication.instance() or QApplication([])
        icon = metronome_icon()
        for mode, state in ((QIcon.Normal, QIcon.Off), (QIcon.Disabled, QIcon.Off), (QIcon.Normal, QIcon.On)):
            self.assertFalse(icon.pixmap(18, 18, mode, state).isNull())

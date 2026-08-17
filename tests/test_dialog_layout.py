from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea

from lumen_reader.dialog_layout import (
    ScreenFittingDialog,
    WheelSafeComboBox,
    WheelSafeDoubleSpinBox,
    WheelSafeFontComboBox,
    WheelSafeSpinBox,
    fitted_rect,
)
from lumen_reader.speed_reader import SpeedReaderSettings, SpeedReaderSettingsDialog


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _send_wheel(widget: object, delta: int = -120) -> None:
    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(widget, event)  # type: ignore[arg-type]


def test_fitted_rect_shrinks_and_centers_inside_usable_screen() -> None:
    fitted = fitted_rect(QRect(0, 0, 900, 800), QRect(100, 50, 700, 500), margin=20)

    assert fitted == QRect(120, 70, 660, 460)


def test_fitted_rect_keeps_smaller_dialog_centered() -> None:
    fitted = fitted_rect(QRect(0, 0, 300, 200), QRect(100, 50, 700, 500), margin=20)

    assert fitted.size().width() == 300
    assert fitted.size().height() == 200
    assert fitted.center() == QRect(100, 50, 700, 500).center()


def test_fitted_rect_clamps_parent_anchor_near_screen_edge() -> None:
    fitted = fitted_rect(
        QRect(0, 0, 300, 200),
        QRect(100, 50, 700, 500),
        margin=20,
        anchor=QPoint(760, 510),
    )

    assert fitted.right() == 779
    assert fitted.bottom() == 529


def test_speed_settings_keeps_buttons_outside_scrollable_content() -> None:
    _application()
    dialog = SpeedReaderSettingsDialog(SpeedReaderSettings())

    scroll = dialog.findChild(QScrollArea, "speedSettingsScroll")
    buttons = dialog.findChild(QDialogButtonBox)
    assert isinstance(dialog, ScreenFittingDialog)
    assert scroll is not None and scroll.widgetResizable()
    assert buttons is not None
    assert scroll.widget().isAncestorOf(dialog.preview)
    assert not scroll.widget().isAncestorOf(buttons)


def test_short_screen_keeps_speed_settings_actions_reachable() -> None:
    app = _application()
    dialog = SpeedReaderSettingsDialog(SpeedReaderSettings())
    dialog._target_screen_geometry = lambda: QRect(0, 0, 800, 600)  # type: ignore[method-assign]
    dialog.show()
    app.processEvents()
    dialog._fit_to_screen()
    app.processEvents()

    scroll = dialog.findChild(QScrollArea, "speedSettingsScroll")
    buttons = dialog.findChild(QDialogButtonBox)
    button_bottom = buttons.mapTo(dialog, QPoint(0, buttons.height())).y()
    assert dialog.frameGeometry().height() <= 600 - 32
    assert button_bottom <= dialog.contentsRect().bottom()
    assert scroll.verticalScrollBar().maximum() > 0
    dialog.close()


def test_settings_fields_ignore_wheel_changes_and_keep_dialog_scrolling() -> None:
    app = _application()
    dialog = SpeedReaderSettingsDialog(SpeedReaderSettings())
    dialog.resize(690, 500)
    dialog.show()
    app.processEvents()

    assert isinstance(dialog.wpm, WheelSafeSpinBox)
    assert isinstance(dialog.clause_factor, WheelSafeDoubleSpinBox)
    assert isinstance(dialog.font_family, WheelSafeFontComboBox)
    scroll = dialog.findChild(QScrollArea, "speedSettingsScroll")
    bar = scroll.verticalScrollBar()
    bar.setValue(0)
    original_wpm = dialog.wpm.value()

    _send_wheel(dialog.wpm)
    app.processEvents()

    assert dialog.wpm.value() == original_wpm
    assert bar.value() > 0
    dialog.close()


def test_closed_combo_box_ignores_wheel_value_changes() -> None:
    _application()
    combo = WheelSafeComboBox()
    combo.addItems(["Night", "Paper", "Sepia"])
    combo.setCurrentIndex(1)

    _send_wheel(combo, 120)

    assert combo.currentText() == "Paper"

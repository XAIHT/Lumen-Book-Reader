"""Viewport-aware sizing and placement for Lumen's custom dialogs."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QWidget


DIALOG_SCREEN_MARGIN = 16


def fitted_rect(
    rect: QRect,
    available: QRect,
    margin: int = DIALOG_SCREEN_MARGIN,
    anchor: QPoint | None = None,
) -> QRect:
    """Return *rect* resized and centered inside a screen's usable geometry."""
    safe = available.adjusted(margin, margin, -margin, -margin)
    if safe.width() <= 0 or safe.height() <= 0:
        safe = QRect(available)
    width = min(rect.width(), safe.width())
    height = min(rect.height(), safe.height())
    result = QRect(0, 0, width, height)
    result.moveCenter(anchor or safe.center())
    if result.left() < safe.left():
        result.moveLeft(safe.left())
    if result.right() > safe.right():
        result.moveRight(safe.right())
    if result.top() < safe.top():
        result.moveTop(safe.top())
    if result.bottom() > safe.bottom():
        result.moveBottom(safe.bottom())
    return result


class ScreenFittingDialog(QDialog):
    """A dialog that remains completely reachable on its parent's monitor."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._natural_minimum: tuple[int, int] | None = None
        self._screen_signal_connected = False
        self._fit_pending = False

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_signal_connected:
            handle.screenChanged.connect(lambda _screen: self._schedule_screen_fit())
            self._screen_signal_connected = True
        self._schedule_screen_fit()

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if event.type() == QEvent.Type.ScreenChangeInternal:
            self._schedule_screen_fit()
        return result

    def _schedule_screen_fit(self) -> None:
        if self._fit_pending:
            return
        self._fit_pending = True
        QTimer.singleShot(0, self._fit_to_screen)

    def _target_screen_geometry(self) -> QRect:
        parent = self.parentWidget()
        screen = None
        if parent is not None:
            center = parent.mapToGlobal(parent.rect().center())
            screen = QGuiApplication.screenAt(center)
            if screen is None and parent.windowHandle() is not None:
                screen = parent.windowHandle().screen()
        if screen is None and self.windowHandle() is not None:
            screen = self.windowHandle().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1024, 768)

    def _anchor(self, available: QRect) -> QPoint:
        parent = self.parentWidget()
        if parent is None or not parent.isVisible():
            return available.center()
        anchor = parent.mapToGlobal(parent.rect().center())
        return anchor if available.contains(anchor) else available.center()

    def _fit_to_screen(self) -> None:
        self._fit_pending = False
        if not self.isVisible() or self.isFullScreen() or self.isMaximized():
            return
        if self._natural_minimum is None:
            self._natural_minimum = (self.minimumWidth(), self.minimumHeight())

        available = self._target_screen_geometry()
        safe = available.adjusted(
            DIALOG_SCREEN_MARGIN,
            DIALOG_SCREEN_MARGIN,
            -DIALOG_SCREEN_MARGIN,
            -DIALOG_SCREEN_MARGIN,
        )
        if safe.width() <= 0 or safe.height() <= 0:
            safe = QRect(available)

        margins = self.windowHandle().frameMargins() if self.windowHandle() is not None else None
        frame_width = max(0, margins.left() + margins.right()) if margins is not None else 0
        frame_height = max(0, margins.top() + margins.bottom()) if margins is not None else 0
        max_width = max(1, safe.width() - frame_width)
        max_height = max(1, safe.height() - frame_height)

        natural_width, natural_height = self._natural_minimum
        self.setMinimumSize(min(natural_width, max_width), min(natural_height, max_height))
        self.resize(min(self.width(), max_width), min(self.height(), max_height))

        frame = fitted_rect(self.frameGeometry(), available, anchor=self._anchor(available))
        self.move(frame.topLeft())

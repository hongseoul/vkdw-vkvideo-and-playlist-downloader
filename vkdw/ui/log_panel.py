"""The log: a drawer that slides in from the right edge of the window."""

from __future__ import annotations

import html
import time

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, FluentIcon, TextBrowser, ToolTipFilter, ToolTipPosition, TransparentToolButton, isDarkTheme

from vkdw.controller import LogEntry
from vkdw.i18n import tr
from vkdw.ui.theme import color
from vkdw.ui.widgets import TextLine

LEVEL_ROLES = {"info": "text3", "ok": "ok", "warn": "warn", "error": "error"}


class LogDrawer(QWidget):
    closed = Signal()

    WIDTH = 440

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[LogEntry] = []
        self._open = False
        self.hide()

        self.heading = TextLine("", 18, QFont.Weight.DemiBold, "text", self)
        self.errors_only = CheckBox("", self)
        self.errors_only.stateChanged.connect(lambda _state: self._render())
        self.copy_button = TransparentToolButton(FluentIcon.COPY, self)
        self.copy_button.clicked.connect(self._copy)
        self.close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_button.clicked.connect(self.close_drawer)
        for button in (self.copy_button, self.close_button):
            button.installEventFilter(ToolTipFilter(button, 300, ToolTipPosition.BOTTOM))

        self.view = TextBrowser(self)
        self.view.setReadOnly(True)
        self.view.setOpenLinks(False)
        self.view.setStyleSheet("TextBrowser, QTextBrowser { background: transparent; border: none; }")

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self.heading, 1)
        head.addWidget(self.errors_only)
        head.addSpacing(6)
        head.addWidget(self.copy_button)
        head.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 12, 12)
        layout.setSpacing(10)
        layout.addLayout(head)
        layout.addWidget(self.view, 1)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(-4, 0)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)

        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._after_slide)
        self.retranslate()

    def retranslate(self) -> None:
        self.heading.setText(tr("log.heading"))
        self.errors_only.setText(tr("log.errors_only"))
        self.copy_button.setToolTip(tr("log.copy"))
        self.close_button.setToolTip(tr("log.close"))

    # Entries

    def add(self, entry: LogEntry) -> None:
        self._entries.append(entry)
        if self._wanted(entry):
            bar = self.view.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 4
            self.view.append(self._line(entry))
            if at_end:
                bar.setValue(bar.maximum())

    def text(self) -> str:
        return "\n".join(entry.text for entry in self._entries)

    def _wanted(self, entry: LogEntry) -> bool:
        return not self.errors_only.isChecked() or entry.level in ("error", "warn")

    def _solid(self, role: str) -> str:
        """Rich text ignores alpha, so mix translucent theme colours with the drawer background."""
        value = color(role)
        back = self._background()
        alpha = value.alphaF()
        mixed = QColor(
            round(value.red() * alpha + back.red() * (1 - alpha)),
            round(value.green() * alpha + back.green() * (1 - alpha)),
            round(value.blue() * alpha + back.blue() * (1 - alpha)),
        )
        return mixed.name()

    @staticmethod
    def _background() -> QColor:
        return QColor(44, 44, 44) if isDarkTheme() else QColor(249, 249, 249)

    def _line(self, entry: LogEntry) -> str:
        dot = self._solid(LEVEL_ROLES.get(entry.level, "text3"))
        muted = self._solid("text3")
        text_color = self._solid("error" if entry.level == "error" else "text")
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.time))
        return (
            f'<p style="margin:0 0 6px 0"><span style="color:{dot}">●</span>&nbsp;'
            f'<span style="color:{muted}">{stamp}</span>&nbsp;&nbsp;'
            f'<span style="color:{text_color}">{html.escape(entry.text)}</span></p>'
        )

    def _render(self) -> None:
        self.view.setHtml("".join(self._line(entry) for entry in self._entries if self._wanted(entry)))
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def refresh_theme(self) -> None:
        self._render()

    def _copy(self) -> None:
        lines = []
        for entry in self._entries:
            if self._wanted(entry):
                stamp = time.strftime("%H:%M:%S", time.localtime(entry.time))
                lines.append(f"{stamp}  {entry.text}")
        QApplication.clipboard().setText("\n".join(lines))

    # Sliding

    def is_open(self) -> bool:
        return self._open

    def open_in(self, area: QRect) -> None:
        self._open = True
        self.setGeometry(area.right() + 1, area.top(), self.WIDTH, area.height())
        self.show()
        self.raise_()
        self._slide(QPoint(area.right() + 1 - self.WIDTH, area.top()))

    def close_drawer(self) -> None:
        if not self._open:
            return
        self._open = False
        self._slide(QPoint(self.parentWidget().width(), self.y()))
        self.closed.emit()

    def place(self, area: QRect) -> None:
        """Keep the open drawer glued to the right edge when the window is resized."""
        if self._open:
            self._anim.stop()
            self.setGeometry(area.right() + 1 - self.WIDTH, area.top(), self.WIDTH, area.height())

    def _slide(self, target: QPoint) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(target)
        self._anim.start()

    def _after_slide(self) -> None:
        if not self._open:
            self.hide()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background())
        painter.setPen(QPen(color("stroke"), 1))
        painter.drawLine(0, 0, 0, self.height())

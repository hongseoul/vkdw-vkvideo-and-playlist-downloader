"""Bottom panel: what is downloading now, how far along, pause and resume."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, ProgressBar, ToolTipFilter, ToolTipPosition, TransparentToolButton

from vkdw.i18n import tr
from vkdw.ui.theme import color
from vkdw.ui.widgets import CoverThumb, TextLine

DOCK_HEIGHT = 78


class DownloadDock(QWidget):
    pauseClicked = Signal()
    resumeClicked = Signal()
    folderClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = "hidden"
        self._shown = False
        self.setMaximumHeight(0)
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.thumb = CoverThumb(QSize(80, 45), 6, self)
        self.title = TextLine("", 14, QFont.Weight.DemiBold, "text", self)
        self.place = TextLine("", 12, QFont.Weight.Normal, "text2", self)
        self.place.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.bar = ProgressBar(self)
        self.bar.setRange(0, 1000)
        self.detail = TextLine("", 12, QFont.Weight.Normal, "text2", self)

        self.pause_button = TransparentToolButton(FluentIcon.PAUSE, self)
        self.pause_button.setFixedSize(40, 40)
        self.pause_button.setIconSize(QSize(18, 18))
        self.pause_button.clicked.connect(self._on_pause)
        self.folder_button = TransparentToolButton(FluentIcon.FOLDER, self)
        self.folder_button.setFixedSize(40, 40)
        self.folder_button.clicked.connect(self.folderClicked)
        self.close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_button.setFixedSize(40, 40)
        self.close_button.setIconSize(QSize(12, 12))
        self.close_button.clicked.connect(self.dismiss)
        for button in (self.pause_button, self.folder_button, self.close_button):
            button.installEventFilter(ToolTipFilter(button, 300, ToolTipPosition.TOP))

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.title, 3)
        top.addWidget(self.place, 2)

        text = QVBoxLayout()
        text.setSpacing(5)
        text.addLayout(top)
        text.addWidget(self.bar)
        text.addWidget(self.detail)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 14, 0)
        layout.setSpacing(14)
        layout.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text, 1)
        layout.addWidget(self.pause_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.folder_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.retranslate()

    def retranslate(self) -> None:
        self.folder_button.setToolTip(tr("dock.open_folder"))
        self.close_button.setToolTip(tr("dock.hide"))
        if self._mode == "paused":
            self.pause_button.setToolTip(tr("dock.resume"))
            self.title.setText(tr("dock.paused"))
        else:
            self.pause_button.setToolTip(tr("dock.pause"))

    # States

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), DOCK_HEIGHT)

    def mode(self) -> str:
        return self._mode

    def show_running(self) -> None:
        self._mode = "running"
        self.pause_button.setIcon(FluentIcon.PAUSE)
        self.pause_button.setToolTip(tr("dock.pause"))
        self.pause_button.setVisible(True)
        self.close_button.setVisible(False)
        self.bar.setPaused(False)
        self.bar.setError(False)
        self.title.setRole("text")
        self._slide(True)

    def show_paused(self, text: str) -> None:
        self._mode = "paused"
        self.pause_button.setIcon(FluentIcon.PLAY)
        self.pause_button.setToolTip(tr("dock.resume"))
        self.pause_button.setVisible(True)
        self.close_button.setVisible(False)
        self.bar.setPaused(True)
        self.title.setText(tr("dock.paused"))
        self.detail.setText(text)
        self._slide(True)

    def show_finished(self, title: str, text: str, ok: bool) -> None:
        self._mode = "finished"
        self.pause_button.setVisible(False)
        self.close_button.setVisible(True)
        self.bar.setPaused(not ok)
        if ok:
            self.bar.setValue(self.bar.maximum())
        self.title.setText(title)
        self.place.setText("")
        self.detail.setText(text)
        self._slide(True)

    def dismiss(self) -> None:
        self._mode = "hidden"
        self._slide(False)

    def set_progress(self, ratio: float) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, ratio)) * self.bar.maximum()))

    def set_text(self, title: str | None = None, place: str | None = None, detail: str | None = None) -> None:
        if title is not None:
            self.title.setText(title)
        if place is not None:
            self.place.setText(place)
        if detail is not None:
            self.detail.setText(detail)

    def status_text(self) -> str:
        return self.detail.text()

    def is_open(self) -> bool:
        return self._shown

    def _on_pause(self) -> None:
        if self._mode == "paused":
            self.resumeClicked.emit()
        else:
            self.pauseClicked.emit()

    def _slide(self, show: bool) -> None:
        if show == self._shown:
            return
        self._shown = show
        self._anim.stop()
        self._anim.setStartValue(self.maximumHeight())
        self._anim.setEndValue(DOCK_HEIGHT if show else 0)
        self._anim.start()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), color("layer"))
        painter.setPen(QPen(color("stroke"), 1))
        painter.drawLine(0, 0, self.width(), 0)

"""Small painted widgets shared by the panels."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from vkdw.ui.theme import color, font, tinted


class TextLine(QWidget):
    """One line of text, cut with … when it does not fit. The colour follows the theme."""

    def __init__(self, text: str = "", size: int = 14, weight=QFont.Weight.Normal, role: str = "text", parent=None):
        super().__init__(parent)
        self._text = text
        self._role = role
        self._align = Qt.AlignmentFlag.AlignLeft
        self.setFont(font(size, weight))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(QFontMetrics(self.font()).height() + 2)

    def set_font(self, size: int, weight=QFont.Weight.Normal) -> None:
        self.setFont(font(size, weight))
        self.setFixedHeight(QFontMetrics(self.font()).height() + 2)
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.updateGeometry()
            self.update()

    def setRole(self, role: str) -> None:
        if role != self._role:
            self._role = role
            self.update()

    def setAlignment(self, align) -> None:
        self._align = align
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self._text) + 2, self.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.height())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setPen(color(self._role))
        painter.setFont(self.font())
        text = QFontMetrics(self.font()).elidedText(self._text, Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(self.rect(), self._align | Qt.AlignmentFlag.AlignVCenter, text)


class Pill(QWidget):
    """A rounded badge: tinted background and text in one colour."""

    def __init__(self, text: str = "", role: str = "text2", parent=None):
        super().__init__(parent)
        self._text = text
        self._role = role
        self.setFont(font(12, QFont.Weight.DemiBold))
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def set(self, text: str, role: str) -> None:
        self._text = text
        self._role = role
        self.setFixedWidth(self.sizeHint().width())
        self.setVisible(bool(text))
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(QFontMetrics(self.font()).horizontalAdvance(self._text) + 20, 22)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_pill(painter, QRectF(self.rect()), self._text, self._role, self.font())


def polished(menu):
    """Give a new menu its own font before items go in.

    qfluentwidgets measures item text while the menu still has the default font, which is
    smaller than the one it draws with, so long items were cut at the right edge.
    """
    menu.view.ensurePolished()
    return menu


def draw_pill(painter: QPainter, rect: QRectF, text: str, role: str, pill_font: QFont) -> None:
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(tinted(role, 36))
    radius = rect.height() / 2
    painter.drawRoundedRect(rect, radius, radius)
    painter.setPen(color(role))
    painter.setFont(pill_font)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


def draw_play_mark(painter: QPainter, rect: QRectF, mark: QColor) -> None:
    side = min(rect.width(), rect.height()) * 0.34
    cx, cy = rect.center().x(), rect.center().y()
    path = QPainterPath()
    path.moveTo(QPointF(cx - side * 0.38, cy - side / 2))
    path.lineTo(QPointF(cx - side * 0.38, cy + side / 2))
    path.lineTo(QPointF(cx + side * 0.5, cy))
    path.closeSubpath()
    painter.fillPath(path, mark)


def draw_thumb(painter: QPainter, rect: QRectF, pixmap: QPixmap | None, radius: float) -> None:
    """A preview, or an empty rounded frame with a play mark while it loads."""
    if pixmap is not None and not pixmap.isNull():
        painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
        return
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color("placeholder"))
    painter.drawRoundedRect(rect, radius, radius)
    draw_play_mark(painter, rect, tinted("text3", 90))
    painter.restore()


class CoverThumb(QWidget):
    """A fixed-size preview image with a placeholder until the image arrives."""

    def __init__(self, size: QSize, radius: float = 6, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._radius = radius
        self.setFixedSize(size)

    def setPixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.update()

    def pixmap(self) -> QPixmap | None:
        return self._pixmap

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        draw_thumb(painter, QRectF(self.rect()), self._pixmap, self._radius)

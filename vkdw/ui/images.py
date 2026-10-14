"""Pillow images to Qt, the blurred cover backdrop, the app icon and the bookmark star."""

from __future__ import annotations

import math

from PIL import Image, ImageEnhance, ImageFilter
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap


def to_pixmap(image: Image.Image, dpr: float = 1.0) -> QPixmap:
    """A Qt pixmap from a Pillow image. dpr > 1 marks it as drawn for a HiDPI screen."""
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
    pixmap = QPixmap.fromImage(qimage)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def backdrop(image: Image.Image) -> QPixmap:
    """A soft, blurred version of a cover to lay behind the playlist header."""
    small = image.convert("RGB")
    small.thumbnail((192, 108), Image.LANCZOS)
    small = ImageEnhance.Color(small).enhance(1.3)
    small = small.filter(ImageFilter.GaussianBlur(14))
    return to_pixmap(small)


def app_icon() -> QIcon:
    icon = QIcon()
    for side in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_icon_pixmap(side))
    return icon


def _icon_pixmap(side: int) -> QPixmap:
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(0, 0, side, side)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, QColor("#2f8cff"))
    gradient.setColorAt(1, QColor("#0059d6"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    radius = side * 0.24
    painter.drawRoundedRect(rect, radius, radius)

    # A play mark over a bar: "video, saved".
    s = side / 32
    play = QPainterPath()
    play.moveTo(QPointF(12.5 * s, 7.5 * s))
    play.lineTo(QPointF(12.5 * s, 19.5 * s))
    play.lineTo(QPointF(22.5 * s, 13.5 * s))
    play.closeSubpath()
    painter.fillPath(play, QColor("white"))
    painter.setBrush(QColor("white"))
    painter.drawRoundedRect(QRectF(8.5 * s, 23 * s, 15 * s, 2.6 * s), 1.3 * s, 1.3 * s)
    painter.end()
    return pixmap


def star_icon(fill: QColor | None, outline: QColor, side: int = 16) -> QIcon:
    """A five-pointed star: filled when fill is given, else only its outline."""
    icon = QIcon()
    for scale in (1, 2, 3):
        icon.addPixmap(_star_pixmap(fill, outline, side * scale, scale))
    return icon


def _star_pixmap(fill: QColor | None, outline: QColor, size: int, scale: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    center = QPointF(size / 2, size / 2 + size * 0.04)
    outer, inner = size * 0.47, size * 0.2
    path = QPainterPath()
    for point in range(10):
        radius = outer if point % 2 == 0 else inner
        angle = math.radians(-90 + point * 36)
        spot = QPointF(center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle))
        path.moveTo(spot) if point == 0 else path.lineTo(spot)
    path.closeSubpath()
    if fill is not None:
        painter.fillPath(path, fill)
    else:
        pen = QPen(outline, 1.25 * scale)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
    painter.end()
    return pixmap

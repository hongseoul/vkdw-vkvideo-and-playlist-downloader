"""Left side of the window: the playlist queue as cards."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    FlyoutViewBase,
    FluentIcon,
    InfoBadge,
    InfoLevel,
    ProgressBar,
    ScrollArea,
    ToolTipFilter,
    ToolTipPosition,
    TransparentPushButton,
    TransparentToolButton,
)

from vkdw.i18n import tr
from vkdw.ui.theme import color
from vkdw.ui.widgets import CoverThumb, Pill, TextLine

# Cards for lists that are open but not queued: the last opened one and the collection.
DRAFT = "draft"
COLLECTION = "collection"


@dataclass
class CardData:
    card_id: str
    title: str
    subtitle: str
    status: str
    role: str
    progress: float | None = None
    cover: QPixmap | None = None
    # Tooltip of the ✕ that removes the card.
    remove_tip: str = ""


class QueueCard(QWidget):
    clicked = Signal(str)
    menuRequested = Signal(str, QPoint)
    removeRequested = Signal(str)

    def __init__(self, card_id: str, parent=None):
        super().__init__(parent)
        self.card_id = card_id
        self._selected = False
        self._hover = False
        self._pressed = False
        self.setFixedHeight(78)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.cover = CoverThumb(QSize(64, 36), 5, self)
        self.title = TextLine("", 13, QFont.Weight.DemiBold, "text", self)
        self.subtitle = TextLine("", 12, QFont.Weight.Normal, "text2", self)
        self.pill = Pill("", "text2", self)
        self.bar = ProgressBar(self)
        self.bar.setMinimumWidth(0)
        self.bar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.bar.setVisible(False)
        self._subtitle = ""
        # Shown while the mouse is over the card.
        self.remove_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.remove_button.setFixedSize(26, 26)
        self.remove_button.setIconSize(QSize(10, 10))
        self.remove_button.setVisible(False)
        self.remove_button.installEventFilter(ToolTipFilter(self.remove_button, 300, ToolTipPosition.TOP))
        self.remove_button.clicked.connect(lambda: self.removeRequested.emit(self.card_id))

        # Either a status badge or, while downloading, a progress bar across the card.
        status = QHBoxLayout()
        status.setSpacing(6)
        status.setContentsMargins(0, 2, 0, 0)
        status.addWidget(self.pill)
        status.addWidget(self.bar, 1)
        status.addStretch(0)

        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        text.addLayout(status)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 8, 8)
        layout.setSpacing(12)
        layout.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text, 1)
        layout.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_data(self, data: CardData) -> None:
        self.title.setText(data.title)
        self._subtitle = data.subtitle
        self.cover.setPixmap(data.cover)
        self.pill.set(data.status, data.role)
        self.set_progress(data.progress)
        self.setToolTip(data.title)
        self.remove_button.setToolTip(data.remove_tip)

    def set_progress(self, ratio: float | None) -> None:
        running = ratio is not None
        self.bar.setVisible(running)
        self.pill.setVisible(not running and bool(self.pill.text()))
        if running:
            value = max(0, min(100, int(ratio * 100)))
            self.bar.setValue(value)
            self.subtitle.setText(f"{self._subtitle} · {value}%")
        else:
            self.subtitle.setText(self._subtitle)

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def enterEvent(self, event) -> None:
        self._hover = True
        self.remove_button.setVisible(True)
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._pressed = False
        self.remove_button.setVisible(False)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self.update()
        if was_pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.card_id)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.menuRequested.emit(self.card_id, event.globalPos())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(4, 2, -4, -2)
        if self._pressed:
            fill = color("pressed")
        elif self._selected:
            fill = color("selected")
        elif self._hover:
            fill = color("hover")
        else:
            fill = None
        painter.setPen(Qt.PenStyle.NoPen)
        if fill is not None:
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 6, 6)
        if self.card_id in (DRAFT, COLLECTION):
            pen = QPen(color("text3"), 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        if self._selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color("accent"))
            painter.drawRoundedRect(QRectF(rect.left() + 1, rect.center().y() - 9, 3, 18), 1.5, 1.5)


class QueuePanel(QWidget):
    cardClicked = Signal(str)
    cardMenu = Signal(str, QPoint)
    cardRemove = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(276)
        self._cards: dict[str, QueueCard] = {}

        self.heading = TextLine("", 16, QFont.Weight.DemiBold, "text", self)
        self.caption = TextLine("", 12, QFont.Weight.Normal, "text2", self)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        head = QHBoxLayout()
        head.setContentsMargins(18, 10, 16, 6)
        head.addWidget(self.heading, 1)
        head.addWidget(self.caption, 1)

        self.container = QWidget()
        self.container.setObjectName("queueContainer")
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(8, 2, 8, 2)
        self.list_layout.setSpacing(2)
        self.hint = BodyLabel("", self.container)
        self.hint.setWordWrap(True)
        self.hint.setTextColor("#727272", "#8a8a8a")
        self.hint.setContentsMargins(10, 6, 10, 6)
        self.list_layout.addWidget(self.hint)
        self.list_layout.addStretch(1)
        self.scroll = ScrollArea(self)
        self.scroll.setWidget(self.container)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.enableTransparentBackground()
        self.container.setStyleSheet("#queueContainer { background: transparent; }")

        self.clear_button = TransparentPushButton(FluentIcon.BROOM, "", self)
        self.log_button = TransparentPushButton(FluentIcon.HISTORY, "", self)
        self.badge = InfoBadge("0", self, InfoLevel.ERROR)
        self.badge.setVisible(False)
        self.settings_button = TransparentToolButton(FluentIcon.SETTING, self)

        footer_row = QHBoxLayout()
        footer_row.setSpacing(2)
        footer_row.addWidget(self.log_button)
        footer_row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        footer_row.addStretch(1)
        footer_row.addWidget(self.settings_button)

        footer = QVBoxLayout()
        footer.setContentsMargins(10, 6, 10, 10)
        footer.setSpacing(2)
        footer.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignLeft)
        footer.addLayout(footer_row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(head)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(footer)
        self.retranslate()

    def retranslate(self) -> None:
        self.heading.setText(tr("queue.heading"))
        self.hint.setText(tr("queue.hint"))
        self.clear_button.setText(tr("queue.clear"))
        self.log_button.setText(tr("log.heading"))
        self.settings_button.setToolTip(tr("settings.tooltip"))

    def card_ids(self) -> list[str]:
        ids = []
        for index in range(self.list_layout.count()):
            widget = self.list_layout.itemAt(index).widget()
            if isinstance(widget, QueueCard):
                ids.append(widget.card_id)
        return ids

    def card(self, card_id: str) -> QueueCard | None:
        return self._cards.get(card_id)

    def selected_id(self) -> str:
        return next((card_id for card_id, card in self._cards.items() if card.is_selected()), "")

    def set_cards(self, rows: list[CardData], selected: str) -> None:
        wanted = {row.card_id for row in rows}
        for card_id in list(self._cards):
            if card_id not in wanted:
                card = self._cards.pop(card_id)
                self.list_layout.removeWidget(card)
                card.hide()
                card.setParent(None)
                card.deleteLater()
        for card in self._cards.values():
            self.list_layout.removeWidget(card)
        for position, row in enumerate(rows):
            card = self._cards.get(row.card_id)
            if card is None:
                card = QueueCard(row.card_id, self.container)
                card.clicked.connect(self.cardClicked)
                card.menuRequested.connect(self.cardMenu)
                card.removeRequested.connect(self.cardRemove)
                self._cards[row.card_id] = card
            card.set_data(row)
            card.set_selected(row.card_id == selected)
            self.list_layout.insertWidget(position, card)
        self.hint.setVisible(not rows)

    def select(self, card_id: str) -> None:
        for key, card in self._cards.items():
            card.set_selected(key == card_id)

    def set_errors(self, count: int) -> None:
        self.badge.setText(str(count))
        self.badge.adjustSize()
        self.badge.setVisible(count > 0)


class BookmarksView(FlyoutViewBase):
    """The bookmarks, as cards like those of the queue: a click opens one, ✕ removes it,
    a right click offers its new videos. Above them: check all of them, download what is new."""

    opened = Signal(str)
    removed = Signal(str)
    menuRequested = Signal(str, QPoint)
    checkAll = Signal()
    downloadAll = Signal()

    WIDTH = 300

    def __init__(self, rows: list[CardData], parent=None):
        super().__init__(parent)
        self._cards: dict[str, QueueCard] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 16, 8, 8)
        layout.setSpacing(6)
        title = TextLine(tr("bookmarks.heading"), 14, QFont.Weight.DemiBold, parent=self)
        self.note = BodyLabel("", self)
        self.note.setWordWrap(True)
        self.note.setFixedWidth(self.WIDTH - 36)
        self.check_button = TransparentPushButton(FluentIcon.SYNC, tr("bookmarks.check_all"), self)
        self.download_button = TransparentPushButton(FluentIcon.DOWNLOAD, "", self)
        self.download_button.setVisible(False)
        self.check_button.clicked.connect(self.checkAll)
        self.download_button.clicked.connect(self.downloadAll)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(10, 0, 10, 0)
        buttons.setSpacing(4)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.download_button)
        buttons.addStretch(1)
        # Text lines up with the titles of the cards below.
        for widget in (title, self.note):
            line = QHBoxLayout()
            line.setContentsMargins(18, 0, 18, 0)
            line.addWidget(widget)
            layout.addLayout(line)
        layout.addLayout(buttons)

        self.container = QWidget()
        self.container.setObjectName("bookmarksContainer")
        self.container.setStyleSheet("#bookmarksContainer { background: transparent; }")
        cards = QVBoxLayout(self.container)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setSpacing(2)
        for row in rows:
            card = QueueCard(row.card_id, self.container)
            card.set_data(row)
            card.clicked.connect(self.opened)
            card.removeRequested.connect(self._remove)
            card.menuRequested.connect(self.menuRequested)
            self._cards[row.card_id] = card
            cards.addWidget(card)
        cards.addStretch(1)
        self.scroll = ScrollArea(self)
        self.scroll.setWidget(self.container)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.enableTransparentBackground()
        self.scroll.setFixedWidth(self.WIDTH)
        layout.addWidget(self.scroll)
        self._fit()

    def card_ids(self) -> list[str]:
        return list(self._cards)

    def card(self, card_id: str) -> QueueCard | None:
        return self._cards.get(card_id)

    def update_rows(self, rows: list[CardData]) -> None:
        """New texts and badges for the cards shown; bookmarks are not added or removed here."""
        for row in rows:
            card = self._cards.get(row.card_id)
            if card is not None:
                card.set_data(row)

    def set_state(self, checking: bool, new: int) -> None:
        """While a check runs its button waits; «Download new» shows when there is something new."""
        self.check_button.setEnabled(not checking and bool(self._cards))
        self.check_button.setText(tr("bookmarks.checking") if checking else tr("bookmarks.check_all"))
        self.download_button.setText(tr("bookmarks.download_new", count=new))
        self.download_button.setVisible(new > 0 and not checking)

    def set_cover(self, card_id: str, pixmap: QPixmap) -> None:
        card = self._cards.get(card_id)
        if card is not None:
            card.cover.setPixmap(pixmap)

    def _remove(self, card_id: str) -> None:
        card = self._cards.pop(card_id, None)
        if card is not None:
            card.hide()
            card.deleteLater()
        self.removed.emit(card_id)
        self._fit()

    def _fit(self) -> None:
        # Up to five cards in sight; more scroll.
        shown = len(self._cards)
        self.scroll.setVisible(shown > 0)
        self.check_button.setVisible(shown > 0)
        if not shown:
            self.download_button.setVisible(False)
        self.scroll.setFixedHeight(min(shown, 5) * 80)
        self.note.setText(tr("bookmarks.note") if shown else tr("bookmarks.empty"))
        self.adjustSize()

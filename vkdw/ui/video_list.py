"""The list of videos: a model, a painted row and a view with tick boxes.

Rows are painted, not built from widgets, so a playlist of thousands of videos
scrolls smoothly. A click anywhere on a row ticks or unticks it.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate
from qfluentwidgets import SmoothScrollDelegate

from vkdw.downloader import EXISTS
from vkdw.i18n import tr
from vkdw.ui.theme import ROW_COLORS, color, font, tinted
from vkdw.ui.widgets import draw_pill, draw_thumb
from vkdw.vk_client import PlaylistInfo, VideoItem, format_duration

ROW_HEIGHT = 64
THUMB_W, THUMB_H = 96, 54

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
INDEX_ROLE = Qt.ItemDataRole.UserRole + 2
CHECKED_ROLE = Qt.ItemDataRole.UserRole + 3
NOTE_ROLE = Qt.ItemDataRole.UserRole + 4
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 5
THUMB_ROLE = Qt.ItemDataRole.UserRole + 6

# The note of a video the archive found saved before, under another name or in another folder.
ELSEWHERE = "elsewhere"


class VideoListModel(QAbstractListModel):
    """Videos of one playlist, which of them are ticked, and their download state."""

    checkedChanged = Signal()

    def __init__(self, thumbs, parent=None):
        super().__init__(parent)
        # thumbs.get(url) returns a pixmap or None and starts loading it.
        self._thumbs = thumbs
        self.playlist: PlaylistInfo | None = None
        self._items: list[VideoItem] = []
        self._visible: list[int] = []
        self._row_of: dict[str, int] = {}
        self._rows_by_thumb: dict[str, list[str]] = {}
        self._notes: dict[str, tuple[str, str]] = {}
        self._progress: dict[str, float] = {}
        self._filter = ""
        self.checked: set[str] = set()
        self.locked = False
        # Videos already saved: in the folder they would be saved to, or found by the archive
        # elsewhere. The latter are also in elsewhere, with their file.
        self.saved: set[str] = set()
        self.elsewhere: dict[str, object] = {}

    # Contents

    def set_playlist(self, playlist: PlaylistInfo, notes: dict, checked, locked: bool, saved=(), elsewhere=None) -> None:
        self.beginResetModel()
        self.playlist = playlist
        self._items = list(playlist.videos)
        self._notes = dict(notes)
        self._progress.clear()
        self.checked = set(checked)
        self.locked = locked
        self.elsewhere = dict(elsewhere or {})
        self.saved = set(saved) | set(self.elsewhere)
        self._rows_by_thumb = {}
        for item in self._items:
            if item.thumb_url:
                self._rows_by_thumb.setdefault(item.thumb_url, []).append(item.raw_id)
        self._apply_filter()
        self.endResetModel()
        self.checkedChanged.emit()

    def clear(self) -> None:
        self.beginResetModel()
        self.playlist = None
        self._items = []
        self._visible = []
        self._row_of = {}
        self._rows_by_thumb = {}
        self._notes = {}
        self._progress.clear()
        self.checked = set()
        self.locked = False
        self.saved = set()
        self.elsewhere = {}
        self.endResetModel()
        self.checkedChanged.emit()

    def set_saved(self, saved, elsewhere=None) -> None:
        elsewhere = dict(elsewhere or {})
        saved = set(saved) | set(elsewhere)
        if saved != self.saved or elsewhere != self.elsewhere:
            self.saved = saved
            self.elsewhere = elsewhere
            if self._visible:
                self.dataChanged.emit(self.index(0), self.index(len(self._visible) - 1), [NOTE_ROLE])

    def set_filter(self, text: str) -> None:
        text = text.strip().casefold()
        if text == self._filter:
            return
        self.beginResetModel()
        self._filter = text
        self._apply_filter()
        self.endResetModel()

    def _apply_filter(self) -> None:
        if self._filter:
            self._visible = [
                pos
                for pos, item in enumerate(self._items)
                if self._filter in item.title.casefold() or self._filter in item.owner_name.casefold()
            ]
        else:
            self._visible = list(range(len(self._items)))
        self._row_of = {self._items[pos].raw_id: row for row, pos in enumerate(self._visible)}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._visible)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._visible):
            return None
        pos = self._visible[index.row()]
        item = self._items[pos]
        if role == ITEM_ROLE:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == INDEX_ROLE:
            return pos + 1
        if role == CHECKED_ROLE:
            return item.raw_id in self.checked
        if role == NOTE_ROLE:
            return self.note(item.raw_id)
        if role == PROGRESS_ROLE:
            return self._progress.get(item.raw_id)
        if role == THUMB_ROLE:
            return self._thumbs.get(item.thumb_url) if item.thumb_url else None
        if role == Qt.ItemDataRole.ToolTipRole:
            note, state = self.note(item.raw_id)
            if state in ("failed", "restricted") and note:
                return f"{item.title}\n{note}"
            if state == "warning":
                return f"{item.title}\n{tr('row.warning_tip', warning=note)}"
            if note == ELSEWHERE and item.raw_id in self.elsewhere:
                return f"{item.title}\n{tr('row.elsewhere_tip', path=self.elsewhere[item.raw_id])}"
            return item.title
        return None

    def note(self, raw_id: str) -> tuple[str, str]:
        item_note = self._notes.get(raw_id)
        if item_note is not None:
            return item_note
        if raw_id in self.elsewhere:
            return ELSEWHERE, "done"
        if raw_id in self.saved:
            return EXISTS, "done"
        row = self._row_of.get(raw_id)
        item = self._items[self._visible[row]] if row is not None else self._find(raw_id)
        if item is not None and item.restriction:
            return item.restriction, "restricted"
        if item is not None and item.warning:
            return item.warning, "warning"
        return "", ""

    def _find(self, raw_id: str) -> VideoItem | None:
        return next((item for item in self._items if item.raw_id == raw_id), None)

    def row_of(self, raw_id: str) -> int | None:
        return self._row_of.get(raw_id)

    def raw_ids(self) -> list[str]:
        return [self._items[pos].raw_id for pos in self._visible]

    # Ticks

    def is_checked(self, row: int) -> bool:
        return self._items[self._visible[row]].raw_id in self.checked

    def toggle(self, row: int) -> None:
        self.set_rows([row], not self.is_checked(row))

    def set_rows(self, rows, value: bool) -> None:
        if self.locked:
            return
        changed = False
        for row in rows:
            raw_id = self._items[self._visible[row]].raw_id
            if value and raw_id not in self.checked:
                self.checked.add(raw_id)
                changed = True
            elif not value and raw_id in self.checked:
                self.checked.discard(raw_id)
                changed = True
        if changed:
            self.dataChanged.emit(self.index(min(rows)), self.index(max(rows)), [CHECKED_ROLE])
            self.checkedChanged.emit()

    def uncheck(self, raw_ids) -> None:
        """Untick these videos, rows hidden by the search included."""
        gone = self.checked & set(raw_ids)
        if self.locked or not gone:
            return
        self.checked -= gone
        if self._visible:
            self.dataChanged.emit(self.index(0), self.index(len(self._visible) - 1), [CHECKED_ROLE])
        self.checkedChanged.emit()

    def set_range(self, first: int, last: int, value: bool) -> None:
        low, high = sorted((first, last))
        self.set_rows(range(low, high + 1), value)

    def set_all_visible(self, value: bool) -> None:
        if self._visible:
            self.set_rows(range(len(self._visible)), value)

    def visible_check_state(self) -> Qt.CheckState:
        ticked = sum(1 for pos in self._visible if self._items[pos].raw_id in self.checked)
        if not ticked:
            return Qt.CheckState.Unchecked
        if ticked == len(self._visible):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def checked_items(self) -> list[tuple[int, VideoItem]]:
        """Ticked (playlist index, video) pairs, in playlist order, hidden by search or not."""
        return [(pos + 1, item) for pos, item in enumerate(self._items) if item.raw_id in self.checked]

    def checked_duration(self) -> int:
        return sum(item.duration for item in self._items if item.raw_id in self.checked)

    def total(self) -> int:
        return len(self._items)

    # Download state

    def set_note(self, raw_id: str, note: str, state: str) -> None:
        self._notes[raw_id] = (note, state)
        if state != "active":
            self._progress.pop(raw_id, None)
        self._changed(raw_id)

    def set_progress(self, raw_id: str, ratio: float) -> None:
        """ratio is 0..1, or -1 while the size is unknown (a stream being assembled)."""
        self._progress[raw_id] = ratio
        self._changed(raw_id)

    def thumb_ready(self, url: str) -> None:
        for raw_id in self._rows_by_thumb.get(url, ()):
            self._changed(raw_id)

    def _changed(self, raw_id: str) -> None:
        row = self._row_of.get(raw_id)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index)


class VideoDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._title_font = font(14, QFont.Weight.DemiBold)
        self._small_font = font(12)
        self._badge_font = font(11, QFont.Weight.DemiBold)
        self._pill_font = font(12, QFont.Weight.DemiBold)

    def sizeHint(self, option, index) -> QSize:
        # Width 0: rows take the view's width and never make it scroll sideways.
        return QSize(0, ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        item: VideoItem = index.data(ITEM_ROLE)
        if item is None:
            return
        checked = bool(index.data(CHECKED_ROLE))
        note, state = index.data(NOTE_ROLE)
        progress = index.data(PROGRESS_ROLE)
        model = index.model()
        locked = getattr(model, "locked", False)

        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(option.rect).adjusted(6, 2, -6, -2)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver) and not locked
        if hovered or state == "active":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tinted("accent", 22) if state == "active" else color("hover"))
            painter.drawRoundedRect(rect, 6, 6)
        view = option.widget
        if view is not None and view.hasFocus() and view.currentIndex() == index:
            painter.setPen(QPen(tinted("accent", 140), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

        x = rect.left() + 12
        cy = rect.center().y()
        self._draw_box(painter, QRectF(x, cy - 9, 18, 18), checked, locked)
        x += 18 + 6

        painter.setFont(self._small_font)
        painter.setPen(color("text3"))
        painter.drawText(QRectF(x, rect.top(), 34, rect.height()), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(index.data(INDEX_ROLE)))
        x += 34 + 12

        painter.setOpacity(1.0 if checked else 0.45)
        thumb_rect = QRectF(x, cy - THUMB_H / 2, THUMB_W, THUMB_H)
        draw_thumb(painter, thumb_rect, index.data(THUMB_ROLE), 6)
        duration = format_duration(item.duration)
        if duration:
            self._draw_duration(painter, thumb_rect, duration)
        x += THUMB_W + 14

        status_w = 160
        status_rect = QRectF(rect.right() - 12 - status_w, rect.top(), status_w, rect.height())
        text_w = max(40.0, status_rect.left() - 12 - x)
        title_metrics = QFontMetrics(self._title_font)
        title = title_metrics.elidedText(item.title, Qt.TextElideMode.ElideRight, int(text_w))
        painter.setFont(self._title_font)
        painter.setPen(color("text"))
        if item.owner_name:
            painter.drawText(QRectF(x, cy - 20, text_w, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
            painter.setFont(self._small_font)
            painter.setPen(color("text2"))
            owner = QFontMetrics(self._small_font).elidedText(item.owner_name, Qt.TextElideMode.ElideRight, int(text_w))
            painter.drawText(QRectF(x, cy + 2, text_w, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, owner)
        else:
            painter.drawText(QRectF(x, rect.top(), text_w, rect.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        painter.setOpacity(1.0)

        self._draw_status(painter, status_rect, note, state, progress)
        painter.restore()

    def _draw_box(self, painter: QPainter, box: QRectF, checked: bool, locked: bool) -> None:
        painter.save()
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tinted("accent", 150) if locked else color("accent"))
            painter.drawRoundedRect(box, 4, 4)
            mark = QPainterPath()
            mark.moveTo(QPointF(box.left() + 4.5, box.center().y() + 0.5))
            mark.lineTo(QPointF(box.left() + 7.7, box.bottom() - 5))
            mark.lineTo(QPointF(box.right() - 4, box.top() + 5.5))
            pen = QPen(Qt.GlobalColor.white, 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(mark)
        else:
            painter.setPen(QPen(tinted("text3", 70) if locked else color("text3"), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box.adjusted(0.6, 0.6, -0.6, -0.6), 4, 4)
        painter.restore()

    def _draw_duration(self, painter: QPainter, thumb: QRectF, text: str) -> None:
        metrics = QFontMetrics(self._badge_font)
        width = metrics.horizontalAdvance(text) + 8
        badge = QRectF(thumb.right() - width - 4, thumb.bottom() - 18 - 4, width, 18)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 175))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(Qt.GlobalColor.white)
        painter.setFont(self._badge_font)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def _draw_status(self, painter: QPainter, rect: QRectF, note: str, state: str, progress) -> None:
        if state == "active":
            if progress is not None and progress >= 0:
                bar = QRectF(rect.left() + 8, rect.center().y() - 2, rect.width() - 56, 4)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color("track"))
                painter.drawRoundedRect(bar, 2, 2)
                painter.setBrush(color("accent"))
                painter.drawRoundedRect(QRectF(bar.left(), bar.top(), max(4.0, bar.width() * progress), bar.height()), 2, 2)
                painter.setFont(self._small_font)
                painter.setPen(color("text2"))
                painter.drawText(
                    QRectF(bar.right(), rect.top(), rect.right() - bar.right(), rect.height()),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{int(progress * 100)}%",
                )
                return
            text = tr("row.assembling") if progress is not None else tr("row.downloading")
            painter.setFont(self._small_font)
            painter.setPen(color("accent"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
            return
        if not state:
            return
        if state == "done":
            if note == EXISTS:
                text = tr("row.exists")
            elif note == ELSEWHERE:
                text = tr("row.elsewhere")
            else:
                text = f"✓ {note or tr('row.done')}"
        elif state == "failed":
            text = tr("row.failed")
        elif state == "stopped":
            text = tr("row.stopped")
        elif state == "restricted":
            text = tr("row.restricted")
        elif state == "warning":
            text = tr("row.age")
        else:
            text = note
        if not text:
            return
        width = QFontMetrics(self._pill_font).horizontalAdvance(text) + 20
        pill = QRectF(rect.right() - width, rect.center().y() - 11, width, 22)
        draw_pill(painter, pill, text, ROW_COLORS.get(state, "text2"), self._pill_font)


class VideoListView(QListView):
    """Click toggles a row, Shift+click ticks a range, Space toggles, Enter downloads."""

    downloadRequested = Signal()
    # A row of a list whose ticks cannot change (a queued playlist) was clicked.
    lockedClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._anchor: int | None = None
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setItemDelegate(VideoDelegate(self))
        self.setStyleSheet("QListView { background: transparent; border: none; outline: none; }")
        self.scrollDelegate = SmoothScrollDelegate(self)
        # The delegate replaces the scroll bar policy setters, so set them after it.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def setModel(self, model) -> None:
        super().setModel(model)
        model.modelReset.connect(self._reset_anchor)

    def _reset_anchor(self) -> None:
        self._anchor = None

    def _press(self, event) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return False
        model = self.model()
        self.setCurrentIndex(index)
        if model.locked:
            self.lockedClicked.emit()
            return True
        row = index.row()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None and self._anchor < model.rowCount():
            model.set_range(self._anchor, row, model.is_checked(self._anchor))
        else:
            model.toggle(row)
            self._anchor = row
        return True

    def mousePressEvent(self, event) -> None:
        if not self._press(event):
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # Two quick clicks are two toggles, not a "double click".
        if not self._press(event):
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        model = self.model()
        if model is None:
            return super().keyPressEvent(event)
        if event.key() == Qt.Key.Key_Space:
            index = self.currentIndex()
            if index.isValid() and model.locked:
                self.lockedClicked.emit()
            elif index.isValid():
                model.toggle(index.row())
                self._anchor = index.row()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.downloadRequested.emit()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            model.set_all_visible(model.visible_check_state() != Qt.CheckState.Checked)
            return
        super().keyPressEvent(event)

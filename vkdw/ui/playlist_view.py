"""Right side of the window: header, toolbar, notices, empty state, loading placeholder."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CheckBox,
    FluentIcon,
    IndeterminateProgressRing,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SearchLineEdit,
    SegmentedWidget,
    SwitchButton,
    ToolTipFilter,
    ToolTipPosition,
    TransparentDropDownPushButton,
    TransparentToolButton,
    isDarkTheme,
)

from vkdw.downloader import QUALITY_CHOICES, quality_label
from vkdw.i18n import number, tr
from vkdw.ui import images
from vkdw.ui.sidebar import QueueCard
from vkdw.ui.theme import color, tinted
from vkdw.ui.widgets import CoverThumb, Pill, TextLine, draw_play_mark, polished

COVER = QSize(160, 90)


def short_path(path: str, parts: int = 2) -> str:
    pieces = Path(path).parts
    if len(pieces) <= parts + 1:
        return str(path)
    return "…\\" + "\\".join(pieces[-parts:])


def tooltip(widget: QWidget, position=ToolTipPosition.BOTTOM) -> None:
    widget.installEventFilter(ToolTipFilter(widget, 300, position))


class ContentPanel(QWidget):
    """The raised layer on the right. Paints the blurred cover of the open playlist on top."""

    RADIUS = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backdrop: QPixmap | None = None
        self._composed: QImage | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_backdrop(self, pixmap: QPixmap | None) -> None:
        if pixmap is self._backdrop:
            return
        self._backdrop = pixmap
        self._composed = None
        self.update()

    def resizeEvent(self, event) -> None:
        self._composed = None
        super().resizeEvent(event)

    def _shape(self, width: float, height: float) -> QPainterPath:
        r = self.RADIUS
        path = QPainterPath()
        path.moveTo(0, height)
        path.lineTo(0, r)
        path.arcTo(QRectF(0, 0, 2 * r, 2 * r), 180, -90)
        path.lineTo(width, 0)
        path.lineTo(width, height)
        path.closeSubpath()
        return path

    def _compose(self) -> QImage:
        width, height = self.width(), min(self.height(), 340)
        dpr = self.devicePixelRatioF()

        def canvas() -> QImage:
            image = QImage(max(1, int(width * dpr)), max(1, int(height * dpr)), QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(Qt.GlobalColor.transparent)
            return image

        # Crop the cover to the panel's proportions, then fade it out downwards.
        source = self._backdrop
        sw, sh = source.width(), source.height()
        aspect = width / max(1, height)
        if sw / max(1, sh) > aspect:
            crop = QRectF((sw - sh * aspect) / 2, 0, sh * aspect, sh)
        else:
            crop = QRectF(0, (sh - sw / aspect) / 2, sw, sw / aspect)
        image = canvas()
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(QRectF(0, 0, width, height), source, crop)

        top = 150 if isDarkTheme() else 105
        fade = QLinearGradient(0, 0, 0, height)
        fade.setColorAt(0, QColor(0, 0, 0, top))
        fade.setColorAt(0.55, QColor(0, 0, 0, top // 2))
        fade.setColorAt(1, QColor(0, 0, 0, 0))
        mask = canvas()
        mask_painter = QPainter(mask)
        mask_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mask_painter.fillPath(self._shape(width, height), fade)
        mask_painter.end()

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, mask)
        painter.end()
        return image

    def refresh(self) -> None:
        self._composed = None
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        width, height = self.width(), self.height()
        painter.fillPath(self._shape(width, height + 1), color("layer"))
        if self._backdrop is not None and not self._backdrop.isNull():
            if self._composed is None:
                self._composed = self._compose()
            painter.drawImage(0, 0, self._composed)
        r = self.RADIUS
        edge = QPainterPath()
        edge.moveTo(0.5, height)
        edge.lineTo(0.5, r)
        edge.arcTo(QRectF(0.5, 0.5, 2 * r, 2 * r), 180, -90)
        edge.lineTo(width, 0.5)
        painter.setPen(QPen(color("stroke"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(edge)


class HeroHeader(QWidget):
    """Cover, title, facts and the download options of the open playlist."""

    folderClicked = Signal()
    refreshClicked = Signal()
    albumChosen = Signal(int)
    bookmarkClicked = Signal()
    coverClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cover = CoverThumb(COVER, 10, self)
        shadow = QGraphicsDropShadowEffect(self.cover)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.cover.setGraphicsEffect(shadow)
        self.kind = TextLine("", 12, QFont.Weight.DemiBold, "text2", self)
        self.title = TextLine("", 26, QFont.Weight.DemiBold, "text", self)
        self.meta = TextLine("", 14, QFont.Weight.Normal, "text2", self)
        self.status = Pill("", "text2", self)
        self.albums_button = TransparentDropDownPushButton(FluentIcon.LIBRARY, "", self)
        self.albums_button.setFixedHeight(28)
        self.albums_button.setVisible(False)
        self._albums: tuple = ()

        self.refresh_button = TransparentToolButton(FluentIcon.SYNC, self)
        self.refresh_button.setFixedSize(36, 36)
        self.refresh_button.clicked.connect(self.refreshClicked)
        tooltip(self.refresh_button)
        self.refresh_ring = IndeterminateProgressRing(self, start=False)
        self.refresh_ring.setFixedSize(20, 20)
        self.refresh_ring.setStrokeWidth(3)
        self.refresh_ring.setVisible(False)
        # Hidden for lists that cannot be read again: a single video or a collection.
        self.bookmark_button = TransparentToolButton(self)
        self.bookmark_button.setFixedSize(36, 36)
        self.bookmark_button.clicked.connect(self.bookmarkClicked)
        self.bookmark_button.setVisible(False)
        tooltip(self.bookmark_button)
        self._bookmarked: bool | None = None
        # Shown for a playlist or a single video: the lists that have a cover of their own.
        self.cover_button = TransparentToolButton(FluentIcon.PHOTO, self)
        self.cover_button.setFixedSize(36, 36)
        self.cover_button.clicked.connect(self.coverClicked)
        self.cover_button.setVisible(False)
        tooltip(self.cover_button)

        self.quality = SegmentedWidget(self)
        for quality, _height in QUALITY_CHOICES:
            self.quality.addItem(quality, quality_label(quality))
        self.quality.setFixedHeight(32)
        self.folder_button = PushButton(FluentIcon.FOLDER, "", self)
        self.folder_button.setMaximumWidth(260)
        tooltip(self.folder_button)
        self.folder_button.clicked.connect(self.folderClicked)
        self.subfolder = SwitchButton(self)
        tooltip(self.subfolder)
        self._compact = False

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.albums_button, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)

        text = QVBoxLayout()
        text.setSpacing(4)
        text.addStretch(1)
        text.addWidget(self.kind)
        text.addWidget(self.title)
        text.addWidget(self.meta)
        text.addSpacing(4)
        text.addLayout(status_row)
        text.addStretch(1)

        side = QVBoxLayout()
        side.setSpacing(0)
        side.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignRight)
        side.addWidget(self.refresh_ring, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(self.bookmark_button, 0, Qt.AlignmentFlag.AlignRight)
        side.addWidget(self.cover_button, 0, Qt.AlignmentFlag.AlignRight)
        side.addStretch(1)

        top = QHBoxLayout()
        top.setSpacing(20)
        top.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(text, 1)
        top.addLayout(side)

        # Download options; a queued playlist keeps its own, so they are hidden there.
        # The folder and the switch move under the quality when the row does not fit.
        self.options = QWidget(self)
        # Its width must not hold the window from shrinking while the row is single.
        self.options.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.target = QWidget(self.options)
        target = QHBoxLayout(self.target)
        target.setContentsMargins(0, 0, 0, 0)
        target.setSpacing(12)
        target.addWidget(self.folder_button)
        target.addWidget(self.subfolder)
        self._options_row = QHBoxLayout()
        self._options_row.setSpacing(12)
        self._options_row.addWidget(self.quality)
        self._options_row.addWidget(self.target)
        self._options_row.addStretch(1)
        self._options = QVBoxLayout(self.options)
        self._options.setContentsMargins(0, 0, 0, 0)
        self._options.setSpacing(10)
        self._options.addLayout(self._options_row)
        self._wrapped = False

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 8, 16, 8)
        self._layout.setSpacing(16)
        self._layout.addLayout(top)
        self._layout.addWidget(self.options)
        self.retranslate()

    def retranslate(self) -> None:
        for quality, _height in QUALITY_CHOICES:
            self.quality.setItemText(quality, quality_label(quality))
        self.subfolder.setOnText(tr("hero.subfolder"))
        self.subfolder.setOffText(tr("hero.subfolder"))
        self.subfolder.setToolTip(tr("hero.subfolder_tip"))
        self.refresh_button.setToolTip(tr("hero.refresh_tip"))
        self.cover_button.setToolTip(tr("hero.cover_tip"))
        self.set_albums(self._albums, force=True)
        self.set_bookmarked(self._bookmarked)
        # The layout would squeeze the segments under the folder button otherwise.
        self.quality.setMinimumWidth(0)
        self.quality.setMinimumWidth(self.quality.sizeHint().width())
        self._fit_options()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_options()

    def _fit_options(self) -> None:
        margins = self._layout.contentsMargins()
        room = self.width() - margins.left() - margins.right()
        spacing = self._options_row.spacing()
        need = (
            self.quality.sizeHint().width()
            + spacing + self.folder_button.sizeHint().width()
            + spacing + self.subfolder.sizeHint().width()
        )
        wrap = need > room
        if wrap == self._wrapped:
            return
        self._wrapped = wrap
        if wrap:
            self._options_row.removeWidget(self.target)
            self._options.addWidget(self.target, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            self._options.removeWidget(self.target)
            self._options_row.insertWidget(1, self.target)

    def refresh_theme(self) -> None:
        self.set_bookmarked(self._bookmarked)

    def set_bookmarked(self, bookmarked: bool | None) -> None:
        """Show the star filled or empty; None hides it. Call again after a theme switch."""
        self._bookmarked = bookmarked
        self.bookmark_button.setVisible(bookmarked is not None)
        if bookmarked is None:
            return
        fill = color("accent") if bookmarked else None
        self.bookmark_button.setIcon(images.star_icon(fill, color("text2")))
        self.bookmark_button.setToolTip(tr("hero.bookmark_remove") if bookmarked else tr("hero.bookmark_add"))

    def set_folder(self, folder: str) -> None:
        text = short_path(folder)
        metrics = self.folder_button.fontMetrics()
        self.folder_button.setText(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, 190))
        self.folder_button.setToolTip(folder)
        self._fit_options()

    def set_albums(self, albums: tuple, force: bool = False) -> None:
        """The playlists of an owner page, offered as a menu next to the status."""
        albums = tuple(albums)
        self.albums_button.setVisible(bool(albums))
        if albums == self._albums and not force:
            return
        self._albums = albums
        old = self.albums_button.menu()
        if old is not None:
            self.albums_button.setMenu(None)
            old.deleteLater()
        if not albums:
            return
        self.albums_button.setText(tr("hero.albums", count=len(albums)))
        menu = polished(RoundMenu(parent=self))
        for album_id, title, amount in albums:
            menu.addAction(
                Action(FluentIcon.VIDEO, f"{title} · {number(amount)}", triggered=lambda _=False, a=album_id: self.albumChosen.emit(a))
            )
        self.albums_button.setMenu(menu)

    def set_refreshing(self, busy: bool) -> None:
        self.refresh_button.setVisible(not busy)
        self.refresh_ring.setVisible(busy)
        if busy:
            self.refresh_ring.start()
        else:
            self.refresh_ring.stop()

    def show_options(self, visible: bool) -> None:
        self.options.setVisible(visible)

    def set_compact(self, compact: bool) -> None:
        """A slim header while the list is scrolled, so more rows fit."""
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (self.cover, self.kind, self.meta):
            widget.setVisible(not compact)
        self.title.set_font(20 if compact else 26, QFont.Weight.DemiBold)
        self._layout.setSpacing(10 if compact else 16)

    def is_compact(self) -> bool:
        return self._compact


class AllCheckBox(CheckBox):
    """The "all" box shows three states but a click is handled by the window."""

    def nextCheckState(self) -> None:
        pass


class ListToolbar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.all_box = AllCheckBox("", self)
        self.search = SearchLineEdit(self)
        self.search.setFixedWidth(280)
        self.counter = TextLine("", 13, QFont.Weight.Normal, "text2", self)
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        # An owner page is read in batches; this asks VK for the next one.
        self.more_button = PushButton(FluentIcon.DOWN, "", self)
        self.more_button.setVisible(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(30, 4, 24, 6)
        layout.setSpacing(16)
        layout.addWidget(self.all_box)
        layout.addWidget(self.search)
        layout.addWidget(self.counter, 1)
        layout.addWidget(self.more_button)
        self._loading = False
        self.retranslate()

    def retranslate(self) -> None:
        self.all_box.setText(tr("list.all"))
        self.search.setPlaceholderText(tr("list.search"))
        self.set_loading_more(self._loading)

    def set_loading_more(self, loading: bool) -> None:
        self._loading = loading
        self.more_button.setText(tr("list.loading_more") if loading else tr("list.more"))
        self.more_button.setEnabled(not loading)


class ActionBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.info = TextLine("", 12, QFont.Weight.Normal, "text2", self)
        self.open_folder = PushButton(FluentIcon.FOLDER, "", self)
        self.retry_button = PushButton(FluentIcon.SYNC, "", self)
        self.new_pick = PrimaryPushButton(FluentIcon.EDIT, "", self)
        tooltip(self.new_pick, ToolTipPosition.TOP)
        self.collect_button = PushButton(FluentIcon.ADD_TO, "", self)
        tooltip(self.collect_button, ToolTipPosition.TOP)
        self.queue_button = PushButton(FluentIcon.ADD, "", self)
        self.download_button = PrimaryPushButton(FluentIcon.DOWNLOAD, "", self)
        self.download_button.setMinimumWidth(150)
        tooltip(self.download_button, ToolTipPosition.TOP)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 10, 24, 16)
        layout.setSpacing(8)
        layout.addWidget(self.info, 1)
        layout.addWidget(self.open_folder)
        layout.addWidget(self.retry_button)
        layout.addWidget(self.new_pick)
        layout.addWidget(self.collect_button)
        layout.addWidget(self.queue_button)
        layout.addWidget(self.download_button)
        self.retranslate()

    def retranslate(self) -> None:
        self.open_folder.setText(tr("actions.open_folder"))
        self.new_pick.setText(tr("actions.new_pick"))
        self.new_pick.setToolTip(tr("actions.new_pick_tip"))
        self.collect_button.setText(tr("actions.collect"))
        self.collect_button.setToolTip(tr("actions.collect_tip"))
        self.queue_button.setText(tr("actions.queue"))

    def show_job_mode(self, job_mode: bool, *, can_collect: bool = True) -> None:
        self.open_folder.setVisible(job_mode)
        self.new_pick.setVisible(job_mode)
        if not job_mode:
            self.retry_button.setVisible(False)
        self.collect_button.setVisible(not job_mode and can_collect)
        self.queue_button.setVisible(not job_mode)
        self.download_button.setVisible(not job_mode)

    def show_retry(self, failed: int) -> None:
        self.retry_button.setVisible(failed > 0)
        self.retry_button.setText(tr("actions.retry", count=failed))


class NoticeArea(QWidget):
    """Notices about the shown list, laid out above it rather than floating over it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 0, 24, 4)
        self._layout.setSpacing(6)
        self._bars: dict[str, InfoBar] = {}
        self._shown: dict[str, tuple] = {}
        self._closed: set[str] = set()
        self._folded = False
        self.setVisible(False)

    def set_folded(self, folded: bool) -> None:
        """Hide the notices while the list is scrolled, like the slim header, so rows have room."""
        self._folded = folded
        self._sync()

    def _sync(self) -> None:
        self.setVisible(bool(self._bars) and not self._folded)

    def show_notice(self, name: str, level: str, title: str, text: str, action: tuple | None = None) -> None:
        """Show (or replace) the notice called name. action is (button text, callback)."""
        if name in self._closed or (name in self._bars and self._shown.get(name) == (level, title, text)):
            return
        self.hide_notice(name)
        self._shown[name] = (level, title, text)
        icon = {"error": InfoBarIcon.ERROR, "warning": InfoBarIcon.WARNING, "success": InfoBarIcon.SUCCESS}.get(
            level, InfoBarIcon.INFORMATION
        )
        bar = InfoBar(
            icon,
            title,
            text,
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            duration=-1,
            position=InfoBarPosition.NONE,
            parent=self,
        )
        # The bar gives spare width to its icon; a stretch keeps the text next to the icon instead.
        bar.hBoxLayout.insertStretch(2, 1)
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, bar.sizePolicy().verticalPolicy())
        if action is not None:
            button = PushButton(action[0], bar)
            button.clicked.connect(action[1])
            bar.addWidget(button)
        bar.closeButton.clicked.connect(lambda _=False, n=name: self._closed.add(n))
        bar.closedSignal.connect(lambda n=name, b=bar: self._forget(n, b))
        self._bars[name] = bar
        self._layout.addWidget(bar)
        self._sync()

    def hide_notice(self, name: str) -> None:
        bar = self._bars.pop(name, None)
        if bar is not None:
            self._layout.removeWidget(bar)
            bar.hide()
            bar.setParent(None)
            bar.deleteLater()
        self._sync()

    def clear(self) -> None:
        """Remove every notice, and let closed ones show again for the next list."""
        for name in list(self._bars):
            self.hide_notice(name)
        self._closed.clear()

    def names(self) -> list[str]:
        return list(self._bars)

    def _forget(self, name: str, bar: InfoBar) -> None:
        if self._bars.get(name) is bar:
            self._bars.pop(name)
        self._sync()


class EmptyArt(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(96, 96)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, tinted("accent", 60))
        gradient.setColorAt(1, tinted("accent", 18))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 26, 26)
        draw_play_mark(painter, rect.adjusted(14, 14, -14, -14), color("accent"))


class EmptyState(QWidget):
    """What to open: examples of links, or, once lists were opened, the latest of them."""

    pasteClicked = Signal()
    recentClicked = Signal(str)
    recentRemoved = Signal(str)

    EXAMPLES = (
        ("vkvideo.ru/playlist/-123_45", "vkvideo.ru/video-123_456"),
        ("vk.com/club123", "vkvideo.ru/@name"),
    )
    # Recent lists shown here, in two columns; the rest are in the menu of the link field.
    RECENT_SHOWN = 4
    CARD_WIDTH = 280

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 24, 40, 40)
        layout.setSpacing(0)
        layout.addStretch(3)
        self.art = EmptyArt(self)
        layout.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(24)
        self.title = TextLine("", 22, QFont.Weight.DemiBold, "text", self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.title)
        layout.addSpacing(8)
        self.body = BodyLabel("", self)
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.body.setTextColor(QColor(0, 0, 0, 158), QColor(255, 255, 255, 200))
        self.body.setFixedWidth(480)
        layout.addWidget(self.body, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(20)
        # Examples of links; they give way to the recent lists once there are some.
        self.examples = QWidget(self)
        examples = QVBoxLayout(self.examples)
        examples.setContentsMargins(0, 0, 0, 0)
        examples.setSpacing(8)
        for row in self.EXAMPLES:
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addStretch(1)
            for example in row:
                pill = Pill("", "text2", self.examples)
                pill.set(example, "text2")
                line.addWidget(pill)
            line.addStretch(1)
            examples.addLayout(line)
        layout.addWidget(self.examples)
        layout.addSpacing(26)
        self.paste_button = PrimaryPushButton(FluentIcon.PASTE, "", self)
        self.paste_button.clicked.connect(self.pasteClicked)
        layout.addWidget(self.paste_button, 0, Qt.AlignmentFlag.AlignHCenter)

        self.recent = QWidget(self)
        self.recent.setFixedWidth(self.CARD_WIDTH * 2 + 8)
        recent = QVBoxLayout(self.recent)
        recent.setContentsMargins(0, 28, 0, 0)
        recent.setSpacing(4)
        self.recent_title = TextLine("", 14, QFont.Weight.DemiBold, "text2", self.recent)
        # Lines up with the titles of the cards below.
        title_line = QHBoxLayout()
        title_line.setContentsMargins(18, 0, 18, 0)
        title_line.addWidget(self.recent_title)
        recent.addLayout(title_line)
        self.recent_grid = QGridLayout()
        self.recent_grid.setContentsMargins(0, 0, 0, 0)
        self.recent_grid.setHorizontalSpacing(8)
        self.recent_grid.setVerticalSpacing(0)
        recent.addLayout(self.recent_grid)
        self._recent_cards: list[QueueCard] = []
        self.recent.setVisible(False)
        layout.addWidget(self.recent, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(4)
        self.retranslate()

    def retranslate(self) -> None:
        self.title.setText(tr("empty.title"))
        self.body.setText(tr("empty.body"))
        self.body.setFixedHeight(self.body.heightForWidth(self.body.width()))
        self.paste_button.setText(tr("empty.paste"))
        self.recent_title.setText(tr("recent.heading"))

    def set_recent(self, rows: list) -> None:
        """Cards of the latest opened lists (CardData): a click opens one, ✕ forgets it."""
        rows = rows[: self.RECENT_SHOWN]
        for card in self._recent_cards:
            self.recent_grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self._recent_cards = []
        for position, row in enumerate(rows):
            card = QueueCard(row.card_id, self.recent)
            card.setFixedWidth(self.CARD_WIDTH)
            card.set_data(row)
            card.clicked.connect(self.recentClicked)
            card.removeRequested.connect(self.recentRemoved)
            self.recent_grid.addWidget(card, position // 2, position % 2)
            self._recent_cards.append(card)
        self.recent.setVisible(bool(rows))
        self.examples.setVisible(not rows)
        self._fit()

    def recent_ids(self) -> list[str]:
        return [card.card_id for card in self._recent_cards]

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        # In a low window the picture gives way, so the recent lists do not overlap.
        needed = self.layout().minimumSize().height() + (self.art.height() if self.art.isHidden() else 0)
        self.art.setHidden(self.height() < needed)


class SkeletonView(QWidget):
    """Grey blocks in the shape of the playlist, with a light sweeping over them."""

    WIDTHS = (0.62, 0.48, 0.71, 0.55, 0.66, 0.43, 0.58, 0.69, 0.5, 0.6)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1400)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._tick)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _tick(self, value) -> None:
        self._phase = float(value)
        self.update()

    def showEvent(self, event) -> None:
        self._anim.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._anim.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        base = color("placeholder")
        light = tinted("text", 28)
        sweep = QLinearGradient((self._phase * 1.6 - 0.4) * width, 0, (self._phase * 1.6 - 0.1) * width, 0)
        sweep.setColorAt(0, base)
        sweep.setColorAt(0.5, light)
        sweep.setColorAt(1, base)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(sweep)

        def block(x, y, w, h, r=4):
            painter.drawRoundedRect(QRectF(x, y, w, h), r, r)

        block(24, 8, COVER.width(), COVER.height(), 10)
        block(204, 30, 90, 10)
        block(204, 48, min(360, width - 240), 24, 6)
        block(204, 82, min(220, width - 240), 12)
        block(24, 118, 320, 30, 6)
        block(360, 118, 160, 30, 6)
        y = 176
        row = 0
        text_w = max(80, width - 420)
        while y < self.height():
            block(36, y + 23, 18, 18, 4)
            block(110, y + 5, 96, 54, 6)
            block(220, y + 18, text_w * self.WIDTHS[row % len(self.WIDTHS)], 12)
            block(220, y + 38, 110, 10)
            y += 64
            row += 1

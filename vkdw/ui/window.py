"""Main window: the queue on the left, the open playlist on the right, the download below."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PIL import Image
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CheckableMenu,
    FluentIcon,
    FluentWidget,
    Flyout,
    FlyoutAnimationType,
    IndeterminateProgressRing,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    LineEdit,
    MenuAnimationType,
    MenuIndicatorType,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SystemThemeListener,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
    qconfig,
)

from vkdw import bookmarks, i18n, session
from vkdw.archive import Archive
from vkdw.controller import DownloadController, playlists_word, saved_ids, target_dir, videos_word
from vkdw.downloader import QUALITY_CHOICES, duplicate_ids, name_width, output_path, quality_label
from vkdw.i18n import count, number, tr
from vkdw.jobs import FINISHED, PENDING, status_label
from vkdw.settings import default_folder, fmt_size, load_settings, save_settings
from vkdw.thumbs import ThumbLoader
from vkdw.ui import images
from vkdw.ui.dock import DownloadDock
from vkdw.ui.log_panel import LogDrawer
from vkdw.ui.playlist_view import (
    COVER,
    ActionBar,
    ContentPanel,
    EmptyState,
    HeroHeader,
    ListToolbar,
    NoticeArea,
    SkeletonView,
    short_path,
)
from vkdw.ui.sidebar import COLLECTION, DRAFT, BookmarksView, CardData, QueuePanel
from vkdw.ui.theme import JOB_COLORS, THEME_MODES, WINDOW_BG, apply_language, apply_theme, color
from vkdw.ui.video_list import INDEX_ROLE, ITEM_ROLE, THUMB_H, THUMB_W, VideoListModel, VideoListView
from vkdw.ui.widgets import TextLine, polished
from vkdw.vk_client import (
    PlaylistInfo,
    Target,
    VideoItem,
    VkError,
    format_duration,
    parse_targets,
    safe_filename,
    video_key,
)

DEFAULT_SIZE = QSize(1180, 780)
MIN_SIZE = QSize(960, 640)
CARD_THUMB = QSize(64, 36)
# Lists with a cover of their own to save: a playlist and a single video.
COVER_KINDS = ("playlist", "video")


# A dropped .txt is read up to this size: a list of links, not a book.
LINKS_FILE_LIMIT = 1024 * 1024


def video_url(item: VideoItem) -> str:
    return f"https://vkvideo.ru/video{item.owner_id}_{item.video_id}"


def read_links_file(path: Path) -> str:
    """The text of a dropped .txt, in UTF-8 or else in the Windows Cyrillic code page."""
    with path.open("rb") as handle:
        raw = handle.read(LINKS_FILE_LIMIT)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", "replace")


# Seconds Windows waits before it shuts down after the queue, so the shutdown can be cancelled.
SHUTDOWN_DELAY = 60


def hint_for(kind: str) -> str:
    """What may help after VK refused (access), could not be reached (network), or a video
    needs ffmpeg that is not there."""
    if kind == "access":
        return tr("hint.access")
    if kind == "network":
        return tr("hint.network")
    if kind == "ffmpeg":
        return tr("hint.ffmpeg")
    return ""


class _ThumbBridge(QObject):
    """Carries finished previews from loader threads to the window thread."""

    ready = Signal(object, object)


class ThumbCache:
    """Video previews as pixmaps. get() starts loading a preview the first time it is asked for."""

    def __init__(self, loader: ThumbLoader, dpr: float):
        self.pixmaps: dict = {}
        self._requested: set[str] = set()
        self._loader = loader
        self._size = (round(THUMB_W * dpr), round(THUMB_H * dpr))
        self._radius = round(6 * dpr)

    def get(self, url: str):
        pixmap = self.pixmaps.get(url)
        if pixmap is None and url and url not in self._requested:
            self._requested.add(url)
            self._loader.request(url, self._size, ("row", url), "rows", radius=self._radius)
        return pixmap

    def forget_requests(self) -> None:
        """Waiting requests were dropped, so ask again when rows are painted."""
        self._requested.clear()


class MainWindow(FluentWidget):
    def __init__(
        self,
        settings: dict | None = None,
        *,
        watch_system_theme: bool = True,
        session_file: Path | None = None,
        archive_file: Path | None = None,
    ):
        super().__init__()
        self._settings = dict(settings if settings is not None else load_settings())
        # Without a file (as in tests) the archive lives in memory only.
        self.controller = DownloadController(Archive(archive_file) if archive_file is not None else None)
        screen = QGuiApplication.primaryScreen()
        self._dpr = max(1.0, min(3.0, screen.devicePixelRatio() if screen else 1.0))
        self._folder = self._settings.get("folder") or str(default_folder())
        qualities = [quality for quality, _height in QUALITY_CHOICES]
        quality = self._settings.get("quality")
        self._quality = quality if quality in qualities else qualities[0]
        self._subfolder = bool(self._settings.get("subfolder", True))
        self._theme_mode = self._settings.get("theme", "auto")

        # What the right side shows: a playlist key, and the queued job it is viewed as ("" = just opened).
        self._shown_key: tuple | None = None
        self._view_job_id = ""
        # The last opened list, and the collection being gathered; both have a card until queued.
        self._draft_key: tuple | None = None
        self._collection_key: tuple | None = None
        self._picks: dict[tuple, set[str]] = {}
        self._covers: dict[tuple, tuple] = {}
        # Covers asked for already, so drawing a list again does not ask twice.
        self._covers_asked: set[tuple] = set()
        self._job_progress: dict[str, float] = {}
        self._current: tuple | None = None
        # The job the download panel last showed, and its folder.
        self._dock_job_id = ""
        self._dock_folder: Path | None = None
        self._errors_unseen = 0
        self._last_offer = ""
        self._page_before_load: QWidget | None = None
        self._syncing = False
        self._tray: QSystemTrayIcon | None = None
        # Shut the computer down once the queue is through. Not saved: it holds for this run only.
        self._shutdown_after = False
        # A queued job whose playlist is being read again before it opens for a new pick.
        self._repick_job_id = ""
        self._locked_hint_shown = False
        self._ids_before_add: set[str] = set()
        # Where the queue is kept between runs (None: not kept), and what was written there last.
        self._session_file = session_file
        self._session_text = ""
        self._bookmarks = bookmarks.from_data(self._settings.get("bookmarks"))
        self._bookmarks_view: BookmarksView | None = None
        self._bookmarks_flyout = None
        # Bookmarks read from VK in this run: key -> "" if read, or what went wrong.
        self._bookmark_checked: dict[tuple, str] = {}
        # Unsaved videos of each checked bookmark: key -> (the list they were found in, pairs).
        # Finding them lists the folder, so it is kept until files may have changed.
        self._unsaved: dict[tuple, tuple] = {}
        # Playlists and pages opened lately, newest first: to get back to one taken out of the queue.
        self._recent = bookmarks.from_data(self._settings.get("recent"))

        self._bridge = _ThumbBridge()
        self._bridge.ready.connect(self._on_thumb)
        self._thumbs = ThumbLoader(self._deliver_thumb)
        self._thumb_cache = ThumbCache(self._thumbs, self._dpr)

        self._build()
        self._restore_geometry()
        self._bind_keys()

        self._theme_listener = None
        if watch_system_theme:
            self._theme_listener = SystemThemeListener(self)
            self._theme_listener.systemThemeChanged.connect(self._on_theme_changed)
            self._theme_listener.start()
        qconfig.themeChanged.connect(lambda _theme: self._on_theme_changed())

        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._pump)
        self._timer.start()
        # Changes are written at most every two seconds, never on each byte of a download.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(2000)
        self._save_timer.timeout.connect(self._save_session)

        self._restore_session()
        self.retranslate()
        if self._settings.get("check_bookmarks") and self._bookmarks:
            QTimer.singleShot(1500, self._check_bookmarks)

    # Layout

    def _build(self) -> None:
        self.setWindowIcon(images.app_icon())
        self.setCustomBackgroundColor(*WINDOW_BG)
        self.setMinimumSize(MIN_SIZE)
        self.setAcceptDrops(True)

        self.sidebar = QueuePanel(self)
        self.panel = ContentPanel(self)
        self.dock = DownloadDock(self)
        self.log_drawer = LogDrawer(self)

        self.link = LineEdit(self.panel)
        self.link.setClearButtonEnabled(True)
        self.link.setFixedHeight(40)
        self.link.addAction(QAction(FluentIcon.LINK.icon(), "", self.link), QLineEdit.ActionPosition.LeadingPosition)
        self.recent_action = QAction(FluentIcon.HISTORY.icon(), "", self.link)
        self.recent_action.triggered.connect(self._recent_menu)
        self.link.addAction(self.recent_action, QLineEdit.ActionPosition.TrailingPosition)
        self.link.returnPressed.connect(self._open_or_cancel)
        self.link.textChanged.connect(lambda _text: self._show_link_error(""))
        self.paste_button = PushButton(FluentIcon.PASTE, "", self.panel)
        self.paste_button.setFixedHeight(40)
        self.paste_button.installEventFilter(ToolTipFilter(self.paste_button, 300, ToolTipPosition.BOTTOM))
        self.paste_button.clicked.connect(self._paste_and_open)
        # Saved lists sit next to the link: both are ways to say what to open.
        self.bookmarks_button = PushButton(self.panel)
        self.bookmarks_button.setFixedHeight(40)
        self.bookmarks_button.installEventFilter(ToolTipFilter(self.bookmarks_button, 300, ToolTipPosition.BOTTOM))
        self.bookmarks_button.clicked.connect(self._show_bookmarks)
        self.bookmarks_star = ToolButton(self.panel)
        self.bookmarks_star.setFixedSize(40, 40)
        self.bookmarks_star.installEventFilter(ToolTipFilter(self.bookmarks_star, 300, ToolTipPosition.BOTTOM))
        self.bookmarks_star.clicked.connect(self._show_bookmarks)
        self.bookmarks_star.setVisible(False)
        self.spinner = IndeterminateProgressRing(self.panel, start=False)
        self.spinner.setFixedSize(22, 22)
        self.spinner.setStrokeWidth(3)
        self.spinner.setVisible(False)
        self.open_button = PrimaryPushButton(FluentIcon.SEARCH, "", self.panel)
        self.open_button.setFixedHeight(40)
        self.open_button.setMinimumWidth(124)
        self.open_button.clicked.connect(self._open_or_cancel)
        self.link_error = TextLine("", 12, role="error", parent=self.panel)
        self.link_error.setVisible(False)
        self.link_hint = TextLine("", 12, role="text2", parent=self.panel)
        self.link_hint.setVisible(False)

        link_row = QHBoxLayout()
        link_row.setContentsMargins(24, 16, 24, 0)
        link_row.setSpacing(8)
        link_row.addWidget(self.link, 1)
        link_row.addWidget(self.paste_button)
        link_row.addWidget(self.bookmarks_button)
        link_row.addWidget(self.bookmarks_star)
        link_row.addWidget(self.spinner)
        link_row.addWidget(self.open_button)
        error_rows = QVBoxLayout()
        error_rows.setContentsMargins(30, 4, 24, 0)
        error_rows.setSpacing(2)
        error_rows.addWidget(self.link_error)
        error_rows.addWidget(self.link_hint)

        self.empty = EmptyState()
        self.empty.pasteClicked.connect(self._paste_and_open)
        self.empty.recentClicked.connect(self._open_recent)
        self.empty.recentRemoved.connect(self._forget_recent)
        self.skeleton = SkeletonView()

        self.hero = HeroHeader()
        self.hero.quality.setCurrentItem(self._quality)
        self.hero.quality.currentItemChanged.connect(self._on_quality)
        self.hero.subfolder.setChecked(self._subfolder)
        self.hero.subfolder.checkedChanged.connect(self._on_subfolder)
        self.hero.set_folder(self._folder)
        self.hero.folderClicked.connect(self._browse)
        self.hero.refreshClicked.connect(self._refresh_shown)
        self.hero.albumChosen.connect(self._open_album)
        self.hero.bookmarkClicked.connect(self._toggle_bookmark)
        self.hero.coverClicked.connect(self._save_list_cover)
        self.notices = NoticeArea()
        self.toolbar = ListToolbar()
        self.toolbar.all_box.clicked.connect(self._toggle_all)
        self.toolbar.search.textChanged.connect(self._on_search)
        self.toolbar.more_button.clicked.connect(self._load_more)
        self.model = VideoListModel(self._thumb_cache, self)
        self.model.checkedChanged.connect(self._on_checked_changed)
        self.list = VideoListView()
        self.list.setModel(self.model)
        self.list.downloadRequested.connect(self._download)
        self.list.lockedClicked.connect(self._explain_locked)
        self.list.customContextMenuRequested.connect(self._row_menu)
        self.list.verticalScrollBar().valueChanged.connect(self._on_list_scroll)
        self.actions = ActionBar()
        self.actions.download_button.clicked.connect(self._download)
        self.actions.queue_button.clicked.connect(self._add_to_queue)
        self.actions.collect_button.clicked.connect(lambda: self._collect())
        self.actions.open_folder.clicked.connect(self._open_job_folder)
        self.actions.new_pick.clicked.connect(self._new_pick)
        self.actions.retry_button.clicked.connect(self._retry_failed)
        self.actions.show_job_mode(False)

        list_wrap = QHBoxLayout()
        list_wrap.setContentsMargins(16, 0, 12, 0)
        list_wrap.addWidget(self.list)
        self.playlist_page = QWidget()
        page = QVBoxLayout(self.playlist_page)
        page.setContentsMargins(0, 8, 0, 0)
        page.setSpacing(4)
        page.addWidget(self.hero)
        page.addWidget(self.notices)
        page.addWidget(self.toolbar)
        page.addLayout(list_wrap, 1)
        page.addWidget(self.actions)

        self.pages = QStackedWidget(self.panel)
        self.pages.setStyleSheet("QStackedWidget { background: transparent; }")
        for widget in (self.empty, self.skeleton, self.playlist_page):
            self.pages.addWidget(widget)
        self.pages.setCurrentWidget(self.empty)

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        panel_layout.addLayout(link_row)
        panel_layout.addLayout(error_rows)
        panel_layout.addWidget(self.pages, 1)

        self.sidebar.cardClicked.connect(self._on_card)
        self.sidebar.cardMenu.connect(self._card_menu)
        self.sidebar.cardRemove.connect(self._remove_card)
        self.sidebar.clear_button.clicked.connect(self._clear_menu)
        self.sidebar.log_button.clicked.connect(self._toggle_log)
        self.sidebar.settings_button.clicked.connect(self._settings_menu)
        self.dock.pauseClicked.connect(self._stop)
        self.dock.resumeClicked.connect(self._resume)
        self.dock.folderClicked.connect(lambda: self._open_folder(self._dock_folder))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.sidebar)
        body.addWidget(self.panel, 1)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, self.titleBar.height(), 0, 0)
        root.setSpacing(0)
        root.addLayout(body, 1)
        root.addWidget(self.dock)
        self.titleBar.raise_()

    def _bind_keys(self) -> None:
        QShortcut(QKeySequence.StandardKey.Paste, self, self._paste_and_open)
        QShortcut(QKeySequence("Ctrl+L"), self, self._focus_link)
        QShortcut(QKeySequence.StandardKey.Find, self, self._focus_search)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, self._refresh_shown)
        QShortcut(QKeySequence("Ctrl+D"), self, self._toggle_bookmark)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.toolbar.search, self.toolbar.search.clear)
        escape.setContext(Qt.ShortcutContext.WidgetShortcut)

    def _restore_geometry(self) -> None:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        geometry = self._settings.get("geometry")
        if isinstance(geometry, list) and len(geometry) == 4 and all(isinstance(v, int) for v in geometry):
            rect = QRect(*geometry)
            if QGuiApplication.screenAt(rect.center()) is not None and rect.width() >= MIN_SIZE.width():
                self.setGeometry(rect)
                return
        width = min(DEFAULT_SIZE.width(), area.width() - 40)
        height = min(DEFAULT_SIZE.height(), area.height() - 40)
        self.resize(max(width, MIN_SIZE.width()), max(height, MIN_SIZE.height()))
        self.move(area.center() - self.rect().center())

    def start_maximized(self) -> bool:
        return bool(self._settings.get("maximized"))

    # Language

    def set_language(self, code: str) -> None:
        apply_language(code)
        self._remember()
        self.retranslate()

    def retranslate(self) -> None:
        """Put every text in the current language; lists and cards are drawn again."""
        self.setWindowTitle(tr("app.title"))
        self.paste_button.setText(tr("link.paste"))
        self.paste_button.setToolTip(tr("link.paste_tip"))
        self.bookmarks_button.setText(tr("bookmarks.heading"))
        self.bookmarks_button.setToolTip(tr("bookmarks.tip"))
        self.bookmarks_star.setToolTip(f'{tr("bookmarks.heading")}: {tr("bookmarks.tip")}')
        self.recent_action.setToolTip(tr("recent.tip"))
        self._paint_bookmarks_icon()
        self._update_bookmarks_view()
        self._update_recent()
        for part in (self.empty, self.hero, self.toolbar, self.actions, self.sidebar, self.dock, self.log_drawer):
            part.retranslate()
        self.hero.set_folder(self._folder)
        self._refresh_queue()
        self._update_counts()
        self.list.viewport().update()

    # Opening links

    def _focus_link(self) -> None:
        self.link.setFocus()
        self.link.selectAll()

    def _focus_search(self) -> None:
        if self.pages.currentWidget() is self.playlist_page:
            self.toolbar.search.setFocus()
            self.toolbar.search.selectAll()

    def _paste_and_open(self) -> None:
        text = QApplication.clipboard().text().strip()
        if not text:
            self._toast("warning", tr("toast.clipboard_empty_title"), tr("toast.clipboard_empty"))
            return
        self.link.setText(text)
        self._open_link()

    def _open_or_cancel(self) -> None:
        if self.controller.loading:
            self.controller.cancel_load()
            return
        self._open_link()

    def _collection_shown(self) -> bool:
        return (
            self._collection_key is not None
            and self._shown_key == self._collection_key
            and not self._view_job_id
            and self.pages.currentWidget() is self.playlist_page
        )

    def _open_link(self) -> None:
        if self.controller.loading:
            return
        try:
            targets = parse_targets(self.link.text().strip())
        except VkError as exc:
            self._show_link_error(str(exc), exc.kind)
            return
        videos = [target for target in targets if target.kind == "video"]
        only_videos = len(videos) == len(targets)
        if only_videos and (len(videos) >= 2 or self._collection_shown()):
            # Several videos gather into a collection; a video pasted while it is open joins it.
            collection = self.controller.playlists.get(self._collection_key) if self._collection_key else None
            self._ids_before_add = {item.raw_id for item in collection.videos} if collection else set()
            self.controller.open_videos(videos, self._collection_key)
            stay = self._collection_shown()
        else:
            self.controller.open_target(targets[0])
            stay = False
        self._show_link_error("")
        if not stay:
            self._show_skeleton()
        self._update_controls()

    def _open_album(self, album_id: int) -> None:
        playlist = self.model.playlist
        if playlist is None or self.controller.loading:
            return
        self.controller.open_target(Target("playlist", playlist.owner_id, album_id))
        self._show_skeleton()
        self._update_controls()

    def _show_skeleton(self) -> None:
        current = self.pages.currentWidget()
        self._page_before_load = current if current is not self.skeleton else self._page_before_load
        self._show_page(self.skeleton)

    def _show_link_error(self, text: str, kind: str = "") -> None:
        hint = hint_for(kind) if text else ""
        self.link.setError(bool(text))
        self.link_error.setText(text)
        self.link_error.setVisible(bool(text))
        self.link_hint.setText(hint)
        self.link_hint.setVisible(bool(hint))

    def _offer_clipboard(self) -> None:
        if self.controller.loading:
            return
        text = QApplication.clipboard().text().strip()
        if not text or len(text) > 4000 or text == self._last_offer or text == self.link.text().strip():
            return
        try:
            targets = parse_targets(text)
        except VkError:
            return
        self._last_offer = text
        videos = [target for target in targets if target.kind == "video"]
        if len(videos) >= 2:
            title = tr("offer.videos", count=count(len(videos), "link"))
            shown = ""
        else:
            target = targets[0]
            key = {
                "playlist": (target.owner_id, target.item_id),
                "video": video_key(target.owner_id, target.item_id),
                "owner": (target.owner_id, "all") if target.owner_id else None,
            }.get(target.kind)
            if key in self.controller.playlists:
                return
            title = tr(f"offer.{target.kind}")
            first = text.split()[0]
            shown = first if len(first) <= 60 else first[:59] + "…"
        bar = InfoBar(
            InfoBarIcon.INFORMATION,
            title,
            shown,
            isClosable=True,
            duration=9000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self.panel,
        )
        button = PushButton(tr("offer.open"), bar)

        def open_offer():
            bar.close()
            self.link.setText(text)
            self._open_link()

        button.clicked.connect(open_offer)
        bar.addWidget(button)
        bar.show()

    # Showing a playlist

    def _show_page(self, page: QWidget) -> None:
        self.pages.setCurrentWidget(page)
        cover = self._covers.get(self._shown_key) if page is self.playlist_page else None
        self.panel.set_backdrop(cover[2] if cover else None)

    def _restore_page(self) -> None:
        page = self._page_before_load
        if page is None or page is self.skeleton or (page is self.playlist_page and self.model.playlist is None):
            page = self.playlist_page if self.model.playlist is not None else self.empty
        self._show_page(page)

    def _on_playlist(self, playlist: PlaylistInfo, mode: str) -> None:
        key = playlist.key
        self._request_cover(playlist)
        if mode in ("open", "refresh", "more"):
            self._refresh_bookmark(playlist, announce=mode == "open")
        if mode == "open":
            self._note_recent(playlist)
        if mode == "add":
            self._on_collection_grew(playlist)
        elif mode == "open":
            self._draft_key = key
            self._picks.pop(key, None)
            self._display(key, "", force=True)
            self.link.clear()
        elif self._repick_job_id and self._repick_job_id == self._view_job_id and mode == "refresh":
            # «Pick again» read the playlist first; now it opens with the fresh list.
            self._repick_job_id = ""
            self._open_draft(key)
        elif key == self._shown_key:
            if not self._view_job_id:
                self._picks[key] = set(self.model.checked)  # rows read just now come unticked
            self.notices.hide_notice("load_failed")
            self._display(key, self._view_job_id, force=True, keep_place=True)
        self._repick_job_id = ""
        self._refresh_queue()
        self._select_view_card()

    def _on_collection_grew(self, playlist: PlaylistInfo) -> None:
        key = playlist.key
        shown = self._collection_shown()
        before = set(self.model.checked) if shown else self._picks.get(key)
        if before is not None:
            # Videos added now are ticked; what was unticked before stays unticked.
            old = self._ids_before_add
            self._picks[key] = before | {item.raw_id for item in playlist.videos if item.raw_id not in old and not item.restriction}
        self._collection_key = key
        self.link.clear()
        if shown:
            self._display(key, "", force=True, keep_place=True)
        else:
            self._display(key, "", force=True)

    def _display(self, key: tuple, job_id: str, force: bool = False, keep_place: bool = False) -> None:
        """Show an opened playlist, as a queued job or as just opened."""
        playlist = self.controller.playlists.get(key)
        if playlist is None:
            return
        same_view = key == self._shown_key and job_id == self._view_job_id
        if not force and same_view:
            if self.pages.currentWidget() is not self.playlist_page and self.pages.currentWidget() is not self.skeleton:
                self._show_page(self.playlist_page)
            return
        if not same_view:
            # Leaving a list that is not queued: keep its ticks for when it is shown again.
            if self.model.playlist is not None and not self._view_job_id and self._shown_key is not None:
                self._picks[self._shown_key] = set(self.model.checked)
            self.notices.clear()
            self._locked_hint_shown = False
        self._view_job_id = job_id
        self._render(playlist, keep_place=keep_place and same_view)
        self._update_controls()

    def _render(self, playlist: PlaylistInfo, keep_place: bool = False) -> None:
        key = playlist.key
        scroll = self.list.verticalScrollBar().value() if keep_place else 0
        search = self.toolbar.search.text() if keep_place else ""
        self._thumbs.drop("rows")
        self._thumb_cache.forget_requests()
        if key != self._shown_key and len(self._thumb_cache.pixmaps) > 1500:
            self._thumb_cache.pixmaps.clear()
        self._shown_key = key
        notes = self.controller.notes.get(key, {})
        ids = {item.raw_id for item in playlist.videos}
        job = self.controller.job(self._view_job_id)
        saved = self._find_saved(playlist)
        elsewhere = self.controller.archive.found(playlist.videos, skip=saved)
        if job is not None:
            wanted = {item.raw_id for _index, item in job.items if item.raw_id in ids}
        elif key in self._picks:
            wanted = {raw_id for raw_id in self._picks[key] if raw_id in ids}
        elif playlist.kind == "owner":
            # A whole community is too much to tick by default; a bookmarked one has its new videos ticked.
            wanted = {item.raw_id for _index, item in self._bookmark_new(key)}
        else:
            # Only what is new: not blocked by VK and not saved yet, here or elsewhere.
            done = saved | set(elsewhere)
            fresh = {item.raw_id for item in playlist.videos if not item.restriction and item.raw_id not in done}
            wanted = fresh if fresh or done else ids
        self.toolbar.search.blockSignals(True)
        self.toolbar.search.setText(search)
        self.toolbar.search.blockSignals(False)
        self.model.set_filter(search)
        self.model.set_playlist(playlist, notes, wanted, locked=job is not None, saved=saved, elsewhere=elsewhere)
        if self._current is not None and self._current[1] == key and self._current[5] is not None:
            self.model.set_progress(self._current[4].raw_id, self._current[5])

        if keep_place:
            self.list.verticalScrollBar().setValue(scroll)
        else:
            active = next((raw_id for raw_id, (_note, state) in notes.items() if state == "active"), "")
            row = self.model.row_of(active) if active else None
            if row is None:
                row = next((r for r, raw_id in enumerate(self.model.raw_ids()) if raw_id in wanted), None)
                if row is not None and row < 6:
                    row = None  # already in sight: keep the list, and the full header, at the top
            if row is not None:
                hint = QAbstractItemView.ScrollHint.PositionAtCenter if active else QAbstractItemView.ScrollHint.PositionAtTop
                self.list.scrollTo(self.model.index(row), hint)
            else:
                self.list.scrollToTop()
                self.hero.set_compact(False)
                self.notices.set_folded(False)
        if self.pages.currentWidget() is not self.skeleton or not self.controller.loading:
            self._show_page(self.playlist_page)
        self._update_header()
        self._update_counts()

    def _target_dir(self, playlist: PlaylistInfo) -> Path:
        return target_dir(Path(self._folder), playlist, self._subfolder)

    def _find_saved(self, playlist: PlaylistInfo) -> set[str]:
        """Videos of the shown list whose file is already where the download would put it."""
        pairs = {item.raw_id: (index, item) for index, item in enumerate(playlist.videos, start=1)}
        job = self.controller.job(self._view_job_id)
        if job is not None:
            if job.target_dir is None:
                return set()
            # A queued job keeps the numbers the videos had when it was queued.
            pairs.update({item.raw_id: (index, item) for index, item in job.items})
            return saved_ids(pairs.values(), job.target_dir, job.naming, job.video_total or job.video_count, job.tagged)
        return saved_ids(pairs.values(), self._target_dir(playlist), playlist.naming, len(playlist.videos))

    def _recheck_saved(self) -> None:
        self._unsaved.clear()
        playlist = self.model.playlist
        if playlist is not None:
            saved = self._find_saved(playlist)
            self.model.set_saved(saved, self.controller.archive.found(playlist.videos, skip=saved))
            self._update_saved_notice()
        self._update_bookmarks_view()

    def _update_header(self) -> None:
        playlist = self.model.playlist
        if playlist is None:
            return
        self.hero.kind.setText(tr(f"kind.{playlist.kind}"))
        self.hero.title.setText(playlist.title)
        self.hero.title.setToolTip(playlist.title)
        parts = [playlist.owner_name] if playlist.owner_name and playlist.kind != "owner" else []
        if playlist.kind == "owner" and playlist.total > len(playlist.videos):
            parts.append(tr("hero.videos_of", shown=number(len(playlist.videos)), total=number(playlist.total)))
        else:
            parts.append(count(len(playlist.videos), "item"))
        duration = format_duration(sum(item.duration for item in playlist.videos))
        if duration:
            parts.append(duration)
        blocked = sum(1 for item in playlist.videos if item.restriction)
        if blocked:
            parts.append(tr("hero.blocked", count=blocked))
        self.hero.meta.setText(" · ".join(parts))
        self.hero.set_albums(playlist.albums)
        self.hero.set_bookmarked(self._bookmark(playlist.key) is not None if playlist.kind in bookmarks.KINDS else None)
        self.hero.cover_button.setVisible(playlist.kind in COVER_KINDS)

        job = self.controller.job(self._view_job_id)
        self._syncing = True
        try:
            if job is not None:
                self.hero.status.set(tr("hero.status_job", status=status_label(job.status)), JOB_COLORS.get(job.status, "text2"))
                self.hero.show_options(False)
                self.actions.info.setText(
                    tr(
                        "actions.job_info",
                        count=videos_word(job.video_count),
                        quality=quality_label(job.quality),
                        folder=short_path(job.folder_label, 3),
                    )
                )
                self.actions.open_folder.setEnabled(bool(job.target_dir))
            else:
                if playlist.key == self._collection_key:
                    text = tr("hero.status_collection")
                elif any(item.playlist_key == playlist.key for item in self.controller.jobs()):
                    text = tr("hero.status_open_queued")
                else:
                    text = tr("hero.status_open")
                self.hero.status.set(text, "text2")
                self.hero.quality.setCurrentItem(self._quality)
                self.hero.set_folder(self._folder)
                self.hero.subfolder.setChecked(self._subfolder)
                self.hero.show_options(True)
                self.actions.info.setText(tr("actions.saves_to", path=short_path(str(self._target_dir(playlist)), 3)))
        finally:
            self._syncing = False
        self.hero.subfolder.setVisible(not playlist.single)
        self.actions.show_job_mode(job is not None, can_collect=playlist.key != self._collection_key)
        failed = len(job.failures) if job is not None and job.status in FINISHED else 0
        if job is not None:
            self.actions.show_retry(failed)
        self.toolbar.more_button.setVisible(playlist.has_more and job is None)
        cover = self._covers.get(playlist.key)
        self.hero.cover.setPixmap(cover[0] if cover else None)
        if self.pages.currentWidget() is self.playlist_page:
            self.panel.set_backdrop(cover[2] if cover else None)
        self._update_notices(playlist, job)

    def _update_notices(self, playlist: PlaylistInfo, job) -> None:
        blocked = sum(1 for item in playlist.videos if item.restriction)
        if job is None and blocked:
            self.notices.show_notice(
                "restricted",
                "warning",
                tr("notice.restricted", count=count(blocked, "video")),
                tr("hint.access"),
            )
        else:
            self.notices.hide_notice("restricted")
        if job is not None and job.status in FINISHED and job.failures:
            kinds = {kind for _raw_id, _text, kind in job.failures}
            lines = [tr("notice.failed_reason", reason=job.failures[0][1])]
            if "access" in kinds:
                lines.append(tr("hint.access_retry"))
            elif "network" in kinds:
                lines.append(tr("hint.network"))
            if "ffmpeg" in kinds:
                lines.append(tr("hint.ffmpeg"))
            lines.append(tr("notice.failed_retry"))
            self.notices.show_notice("failed", "error", tr("notice.failed", count=count(len(job.failures), "video")), "\n".join(lines))
        else:
            self.notices.hide_notice("failed")
        self._update_saved_notice()

    def _update_saved_notice(self) -> None:
        """Say how many videos are already in the folder, before anything downloads."""
        playlist = self.model.playlist
        job = self.controller.job(self._view_job_id)
        if playlist is None or (job is not None and job.status not in PENDING):
            # While a job runs or after it, its rows show what happened to each file.
            self.notices.hide_notice("saved")
            return
        elsewhere = set(self.model.elsewhere)
        if job is not None:
            among = {item.raw_id for _index, item in job.items}
            # The download skips only files in its own folder; the others it saves again.
            saved = (self.model.saved - elsewhere) & among
        else:
            among = {item.raw_id for item in playlist.videos}
            saved = self.model.saved & among
        if not saved:
            self.notices.hide_notice("saved")
            return
        everything = len(saved) == len(among)
        title = tr("notice.saved_all") if everything else tr("notice.saved_some", count=number(len(saved)), total=number(len(among)))
        action = None
        if job is not None:
            text = tr("notice.saved_job")
        else:
            ticked = saved & self.model.checked
            if ticked - elsewhere:
                text = tr("notice.saved_ticked", count=number(len(ticked - elsewhere)))
                action = (tr("notice.untick_saved"), lambda: self.model.uncheck(self.model.saved))
            elif ticked:
                text = tr("notice.elsewhere_ticked", count=number(len(ticked)))
                action = (tr("notice.untick_saved"), lambda: self.model.uncheck(self.model.saved))
            elif saved & elsewhere:
                text = tr("notice.saved_elsewhere")
            elif everything:
                text = tr("notice.saved_where", path=short_path(str(self._target_dir(playlist)), 3))
            else:
                text = tr("notice.saved_unticked")
        self.notices.show_notice("saved", "info", title, text, action)

    def _update_counts(self) -> None:
        picked = len(self.model.checked)
        total = self.model.total()
        text = tr("list.counter", picked=number(picked), total=number(total))
        duration = format_duration(self.model.checked_duration())
        if picked and duration:
            text += f" · {duration}"
        playlist = self.model.playlist
        if playlist is not None and playlist.has_more:
            text += " · " + tr("list.in_vk", total=number(playlist.total))
        self.toolbar.counter.setText(text)
        self.toolbar.all_box.setCheckState(self.model.visible_check_state())
        self.actions.download_button.setText(tr("actions.download_n", count=picked) if picked else tr("actions.download"))

    def _on_checked_changed(self) -> None:
        self._update_counts()
        self._update_controls()
        self._update_saved_notice()
        self._schedule_save()

    def _on_list_scroll(self, value: int) -> None:
        # The header slims down while the list is scrolled and comes back at the top.
        if value > 24 and not self.hero.is_compact():
            self.hero.set_compact(True)
            self.notices.set_folded(True)
        elif value == 0 and self.hero.is_compact():
            self.hero.set_compact(False)
            self.notices.set_folded(False)

    def _on_search(self, text: str) -> None:
        self.model.set_filter(text)
        self.toolbar.all_box.setCheckState(self.model.visible_check_state())

    def _toggle_all(self) -> None:
        if self.model.locked:
            self._explain_locked()
            return
        self.model.set_all_visible(self.model.visible_check_state() != Qt.CheckState.Checked)
        self.toolbar.all_box.setCheckState(self.model.visible_check_state())

    def _explain_locked(self) -> None:
        if self._locked_hint_shown:
            return
        self._locked_hint_shown = True
        self._toast("info", tr("toast.locked_title"), tr("toast.locked"), duration=6000)

    def _update_controls(self) -> None:
        loading = self.controller.loading
        mode = self.controller.load_mode
        collecting = self._collection_shown()
        if loading:
            self.open_button.setText(tr("link.cancel"))
            self.open_button.setIcon(FluentIcon.CANCEL)
        elif collecting:
            self.open_button.setText(tr("link.add"))
            self.open_button.setIcon(FluentIcon.ADD)
        else:
            self.open_button.setText(tr("link.open"))
            self.open_button.setIcon(FluentIcon.SEARCH)
        self.link.setPlaceholderText(tr("link.placeholder_collection") if collecting else tr("link.placeholder"))
        self.spinner.setVisible(loading)
        if loading:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.paste_button.setEnabled(not loading)
        shown_busy = loading and self.controller.load_key == self._shown_key and self._shown_key is not None
        self.hero.set_refreshing(shown_busy and mode == "refresh")
        self.hero.refresh_button.setEnabled(not loading)
        self.toolbar.set_loading_more(shown_busy and mode == "more")
        picked = bool(self.model.checked)
        can_pick = self.model.playlist is not None and not loading and not self._view_job_id
        downloading = self.controller.downloading
        self.actions.download_button.setEnabled(can_pick and picked and not downloading)
        self.actions.download_button.setToolTip(tr("actions.download_busy_tip") if downloading else tr("actions.download_tip"))
        self.actions.queue_button.setEnabled(can_pick and picked)
        self.actions.collect_button.setEnabled(self.model.playlist is not None and picked and not self._view_job_id)
        self.actions.new_pick.setEnabled(not loading)
        self.toolbar.all_box.setEnabled(self.model.playlist is not None)
        if downloading and self.dock.mode() != "running":
            self.dock.show_running()
            self.dock.set_text(title=tr("dock.preparing"), place="", detail="")
            self.dock.set_progress(0)

    # Reading a list again

    def _refresh_shown(self) -> None:
        key = self._shown_key
        if key is None or self.controller.loading or self.pages.currentWidget() is not self.playlist_page:
            return
        if not self._view_job_id:
            self._picks[key] = set(self.model.checked)
        if self.controller.refresh(key):
            self.notices.hide_notice("load_failed")
        self._update_controls()

    def _load_more(self) -> None:
        key = self._shown_key
        if key is None or self._view_job_id:
            return
        self._picks[key] = set(self.model.checked)
        if self.controller.load_more(key):
            self.notices.hide_notice("load_failed")
        self._update_controls()

    def _on_fatal(self, text: str, kind: str, mode: str, key) -> None:
        self._repick_job_id = ""
        if self.pages.currentWidget() is self.skeleton:
            self._restore_page()
        if mode in ("open", "add"):
            self._show_link_error(text, kind)
            hint = hint_for(kind)
            self._toast("error", tr("toast.open_failed"), f"{text}\n{hint}" if hint else text, duration=7000)
        elif key == self._shown_key:
            title = tr("notice.more_failed") if mode == "more" else tr("notice.refresh_failed")
            hint = hint_for(kind)
            self.notices.show_notice("load_failed", "error", title, f"{text}\n{hint}" if hint else text)
        else:
            self._toast("error", tr("notice.refresh_failed"), text, duration=7000)

    # Collections

    def _collect(self, items: list[VideoItem] | None = None) -> None:
        """Put videos of the shown list into the collection: the ticked ones, or those given."""
        if self.model.playlist is None:
            return
        if items is None:
            items = [item for _index, item in self.model.checked_items()]
        if not items:
            self._toast("info", tr("toast.nothing_title"), tr("toast.nothing"))
            return
        info, added = self.controller.add_to_collection(self._collection_key, items)
        self._collection_key = info.key
        if info.key in self._picks:
            self._picks[info.key] |= {item.raw_id for item in items}
        self._request_cover(info)
        self._refresh_queue()
        if added:
            self._toast("success", tr("toast.collected_title"), tr("toast.collected", count=videos_word(added), title=info.title))
        else:
            self._toast("info", tr("toast.collected_title"), tr("toast.already_collected"))

    def _uncollect(self, raw_ids: list[str]) -> None:
        key = self._collection_key
        if key is None:
            return
        if self._shown_key == key:
            self._picks[key] = set(self.model.checked) - set(raw_ids)
        info = self.controller.remove_from_collection(key, raw_ids)
        if info is None or not info.videos:
            self._drop_collection()
            return
        if self._shown_key == key and not self._view_job_id:
            self._display(key, "", force=True, keep_place=True)
        self._refresh_queue()

    def _ask_drop_collection(self) -> None:
        collection = self.controller.playlists.get(self._collection_key) if self._collection_key is not None else None
        if collection is not None and len(collection.videos) > 1:
            text = tr("collection.confirm_text", title=collection.title, count=videos_word(len(collection.videos)))
            if not self._ask(tr("collection.confirm_title"), text, tr("collection.confirm_yes")):
                return
        self._drop_collection()

    def _drop_collection(self) -> None:
        key = self._collection_key
        self._collection_key = None
        if key is not None and self._shown_key == key and not self._view_job_id:
            self.model.clear()
            self.notices.clear()
            self._shown_key = None
            self._show_page(self.empty)
        self._refresh_queue()
        self._update_controls()

    # Queue

    def _download(self) -> None:
        button = self.actions.download_button
        if button.isEnabled() and not button.isHidden():
            self._enqueue(start=True)

    def _add_to_queue(self) -> None:
        if self.actions.queue_button.isHidden():
            return
        self._enqueue(start=False)

    def _collect_job(self):
        playlist = self.model.playlist
        if playlist is None:
            self._toast("info", tr("toast.no_list_title"), tr("toast.no_list"))
            return None
        items = self.model.checked_items()
        if not items:
            self._toast("info", tr("toast.nothing_title"), tr("toast.nothing"))
            return None
        if not self._folder.strip():
            self._toast("error", tr("toast.no_folder_title"), tr("toast.no_folder"))
            return None
        self._remember()
        return self.controller.make_job(playlist, items, Path(self._folder), self._quality, self._subfolder)

    def _enqueue(self, *, start: bool) -> None:
        job = self._collect_job()
        if job is None:
            return
        self._place(job, start=start)

    def _place(self, job, *, start: bool) -> None:
        placed, message = self.controller.enqueue(job, start=start)
        if job.playlist_key == self._collection_key:
            self._collection_key = None  # a queued collection is fixed; the next one starts empty
        if job.playlist_key == self._shown_key and not self._view_job_id:
            # The list now shows what was just queued, with its progress.
            self._view_job_id = job.job_id
            self.model.locked = True
            self._locked_hint_shown = False
            self.list.viewport().update()
        elif job.playlist_key == self._shown_key:
            self._display(job.playlist_key, job.job_id, force=True)
        self._refresh_queue()
        self._select_view_card()
        title = tr("toast.started") if placed == "now" else tr("toast.queued")
        self._toast("success", title, message)

    def _retry_failed(self) -> None:
        job = self.controller.retry_failed(self._view_job_id)
        if job is not None:
            self._place(job, start=True)

    def _resume(self) -> None:
        self.controller.resume()
        self._refresh_queue()
        self._update_controls()

    def _stop(self) -> None:
        self.controller.stop()
        self.dock.set_text(detail=tr("status.stopping"))

    def _remove_jobs(self, job_ids: list[str]) -> None:
        running = next((job for job in self.controller.jobs() if job.job_id in job_ids and job.status == "running"), None)
        if running is not None and not self._ask(tr("remove.title"), tr("remove.text", title=running.title), tr("remove.yes")):
            return
        keys = self._job_keys(job_ids)
        self.controller.remove(job_ids)
        self._keep_view(keys)
        self._refresh_queue()

    def _remove_card(self, card_id: str) -> None:
        if card_id == DRAFT:
            self._drop_draft()
        elif card_id == COLLECTION:
            self._ask_drop_collection()
        else:
            self._remove_jobs([card_id])

    def _clear_menu(self) -> None:
        finished = any(job.status in FINISHED for job in self.controller.jobs())
        menu = polished(RoundMenu(parent=self))
        done = Action(FluentIcon.ACCEPT, tr("queue.clear_finished"), triggered=self._clear_finished)
        done.setEnabled(finished)
        menu.addAction(done)
        menu.addAction(Action(FluentIcon.DELETE, tr("queue.clear_all"), triggered=self._clear_all))
        self._popup_above(menu, self.sidebar.clear_button)

    def _clear_finished(self) -> None:
        keys = {job.playlist_key for job in self.controller.jobs() if job.status in FINISHED}
        self.controller.clear_finished()
        self._keep_view(keys)
        self._refresh_queue()

    def _clear_all(self) -> None:
        unfinished = [job for job in self.controller.jobs() if job.status not in FINISHED]
        if unfinished:
            running = any(job.status == "running" for job in unfinished)
            text = tr("queue.confirm_running" if running else "queue.confirm_text", count=playlists_word(len(unfinished)))
            if not self._ask(tr("queue.confirm_title"), text, tr("queue.confirm_yes")):
                return
        keys = self._job_keys([job.job_id for job in self.controller.jobs()])
        self.controller.clear()
        self._keep_view(keys)
        self._refresh_queue()

    def _job_keys(self, job_ids) -> set[tuple]:
        return {job.playlist_key for job in self.controller.jobs() if job.job_id in job_ids}

    def _keep_view(self, removed_keys: set[tuple]) -> None:
        """After jobs were removed, their lists go too: no "not queued" card comes back for
        something just taken out. Only a list open on the right apart from the queue stays."""
        open_apart = self._shown_key if not self._view_job_id else None
        if self._view_job_id and self.controller.job(self._view_job_id) is None:
            self._view_job_id = ""
            self.model.clear()
            self.notices.clear()
            self._shown_key = None
            self._show_page(self.empty)
        if self._draft_key in removed_keys and self._draft_key != open_apart:
            self._draft_key = None

    def _new_pick(self) -> None:
        """Read the queued playlist from VK again, then open it for a new pick with the old ticks."""
        job = self.controller.job(self._view_job_id)
        if job is None:
            return
        self._picks[job.playlist_key] = {item.raw_id for _index, item in job.items}
        if self.controller.refresh(job.playlist_key):
            self._repick_job_id = job.job_id
            self.notices.hide_notice("load_failed")
            self._update_controls()
        else:
            self._open_draft(job.playlist_key)

    def _open_draft(self, key: tuple) -> None:
        self._draft_key = key
        self._display(key, "", force=True)
        self._refresh_queue()
        self._select_view_card()

    def _on_card(self, card_id: str) -> None:
        if card_id == DRAFT:
            if self._draft_key is not None:
                self._display(self._draft_key, "")
        elif card_id == COLLECTION:
            if self._collection_key is not None:
                self._display(self._collection_key, "")
        else:
            job = self.controller.job(card_id)
            if job is not None:
                self._display(job.playlist_key, job.job_id)
        self._select_view_card()
        self._refresh_queue()

    def _select_view_card(self) -> None:
        self.sidebar.select(self._view_card())

    def _view_card(self) -> str:
        if self._view_job_id:
            return self._view_job_id
        if self._shown_key is not None and self._shown_key == self._collection_key:
            return COLLECTION
        if self._shown_key is not None and self._shown_key == self._draft_key:
            return DRAFT
        return ""

    def _card_menu(self, card_id: str, pos: QPoint) -> None:
        menu = polished(RoundMenu(parent=self))
        if card_id in (DRAFT, COLLECTION):
            menu.addAction(Action(FluentIcon.VIEW, tr("menu.show"), triggered=lambda: self._on_card(card_id)))
            if card_id == DRAFT:
                menu.addAction(Action(FluentIcon.CLOSE, tr("menu.hide"), triggered=self._drop_draft))
            else:
                menu.addAction(Action(FluentIcon.DELETE, tr("menu.drop_collection"), triggered=self._ask_drop_collection))
        else:
            job = self.controller.job(card_id)
            if job is None:
                return
            menu.addAction(Action(FluentIcon.VIEW, tr("menu.show"), triggered=lambda: self._on_card(card_id)))
            menu.addAction(Action(FluentIcon.DELETE, tr("menu.remove_job"), triggered=lambda: self._remove_jobs([card_id])))
            folder = Action(FluentIcon.FOLDER, tr("menu.open_folder"), triggered=lambda: self._open_folder(job.target_dir))
            folder.setEnabled(bool(job.target_dir))
            menu.addAction(folder)
        menu.exec(pos)

    def _drop_draft(self) -> None:
        if self._shown_key == self._draft_key and not self._view_job_id:
            self.model.clear()
            self.notices.clear()
            self._shown_key = None
            self._show_page(self.empty)
        self._draft_key = None
        self._refresh_queue()
        self._update_controls()

    def _refresh_queue(self) -> None:
        jobs = self.controller.jobs()
        queued_keys = {job.playlist_key for job in jobs}
        rows = []
        for job in jobs:
            cover = self._covers.get(job.playlist_key)
            progress = self._job_progress.get(job.job_id, 0.0) if job.status == "running" else None
            rows.append(
                CardData(
                    job.job_id,
                    job.title,
                    f"{videos_word(job.video_count)} · {quality_label(job.quality)}",
                    tr(f"card.{job.status}"),
                    JOB_COLORS.get(job.status, "text2"),
                    progress,
                    cover[1] if cover else None,
                    tr("menu.remove_job"),
                )
            )
        draft = None
        if self._draft_key is not None and self._draft_key not in queued_keys and self._draft_key != self._collection_key:
            draft = self.controller.playlists.get(self._draft_key)
        if draft is not None:
            # The last opened playlist stays one click away until it is queued.
            cover = self._covers.get(draft.key)
            rows.append(
                CardData(
                    DRAFT,
                    draft.title,
                    count(len(draft.videos), "item"),
                    tr("card.not_queued"),
                    "text3",
                    None,
                    cover[1] if cover else None,
                    tr("menu.hide"),
                )
            )
        collection = self.controller.playlists.get(self._collection_key) if self._collection_key is not None else None
        if collection is not None:
            cover = self._covers.get(collection.key)
            rows.append(
                CardData(
                    COLLECTION,
                    collection.title,
                    count(len(collection.videos), "item"),
                    tr("card.collection"),
                    "accent",
                    None,
                    cover[1] if cover else None,
                    tr("menu.drop_collection"),
                )
            )
        self.sidebar.set_cards(rows, self._view_card())
        self.sidebar.clear_button.setVisible(bool(jobs))

        waiting = sum(1 for job in jobs if job.status == "waiting")
        running = any(job.status == "running" for job in jobs)
        held = any(job.status == "interrupted" for job in jobs)
        if not jobs:
            caption = tr("queue.caption.empty")
        elif running and waiting:
            caption = tr("queue.caption.running_more", count=waiting)
        elif running:
            caption = tr("queue.caption.running")
        elif self.controller.paused or held:
            caption = tr("queue.caption.paused")
        elif waiting:
            caption = tr("queue.caption.waiting", count=waiting)
        else:
            caption = tr("queue.caption.finished")
        self.sidebar.caption.setText(caption)
        self._sync_dock()
        self._schedule_save()
        self._update_header()
        self._update_controls()

    def _sync_dock(self) -> None:
        """Keep a paused download panel true to the queue after playlists were added or removed."""
        if self.dock.mode() != "paused":
            return
        left = self.controller.pending_count()
        if not left:
            self.dock.dismiss()
            return
        self.dock.set_text(detail=tr("idle.paused", left=playlists_word(left)))
        if self._dock_job_id and self.controller.job(self._dock_job_id) is None:
            # The playlist it stopped on is gone; the next one starts from its first video.
            self._dock_job_id = ""
            self.dock.set_text(title=tr("dock.paused"), place="")
            self.dock.set_progress(0)
            self.dock.thumb.setPixmap(None)

    # Events from the controller

    def _pump(self) -> None:
        events = self.controller.drain()
        for event in events:
            self._dispatch(event)
        if events:
            self._update_controls()

    def _dispatch(self, event: tuple) -> None:
        kind = event[0]
        if kind == "playlist":
            self._on_playlist(event[1], event[2])
        elif kind == "fatal":
            self._on_fatal(*event[1:])
        elif kind == "load_done":
            self._repick_job_id = ""
            if self.pages.currentWidget() is self.skeleton:
                self._restore_page()
        elif kind == "log":
            entry = event[1]
            self.log_drawer.add(entry)
            if entry.level == "error" and not self.log_drawer.is_open():
                self._errors_unseen += 1
                self.sidebar.set_errors(self._errors_unseen)
        elif kind == "status":
            self.dock.set_text(detail=event[1])
        elif kind == "queue":
            self._refresh_queue()
        elif kind == "idle":
            self._on_idle(event[1], event[2])
        elif kind == "row":
            _kind, key, raw_id, note, state = event
            if state == "done":
                self._unsaved.clear()  # a file was saved: it may be in any bookmark
            if key == self._shown_key:
                self.model.set_note(raw_id, note, state)
        elif kind == "overall":
            self._on_overall(*event[1:])
        elif kind == "file":
            self._on_file(*event[1:])
        elif kind == "checked":
            self._on_checked(*event[1:])
        elif kind == "check_done":
            self._on_check_done(*event[1:])
        elif kind == "covers_saved":
            self._on_covers_saved(*event[1:])

    def _on_overall(self, job_id: str, key: tuple, position: int, total: int, item: VideoItem) -> None:
        self._current = (job_id, key, position, total, item, None)
        ratio = (position - 1) / max(total, 1)
        self._job_progress[job_id] = ratio
        card = self.sidebar.card(job_id)
        if card is not None:
            card.set_progress(ratio)
        job = self.controller.job(job_id)
        if job is not None:
            self._dock_folder = job.target_dir
        self._dock_job_id = job_id
        self.dock.show_running()
        self.dock.set_text(title=item.title, place=self._place_text(job_id, position, total), detail=tr("dock.connecting"))
        self.dock.set_progress(ratio)
        self.dock.thumb.setPixmap(self._thumb_cache.get(item.thumb_url) if item.thumb_url else None)

    def _place_text(self, job_id: str, position: int, total: int) -> str:
        index, amount = self.controller.position(job_id)
        if amount > 1 and index:
            return tr("dock.place", index=index, count=amount, position=position, total=total)
        return tr("dock.place_video", position=position, total=total)

    def _on_file(self, job_id: str, key: tuple, raw_id: str, loaded: int, size: int, speed: float) -> None:
        current = self._current
        if current is None or current[0] != job_id:
            return
        position, total, item = current[2], current[3], current[4]
        if size:
            ratio = min(loaded / size, 1.0)
            detail = tr("dock.bytes_of", loaded=fmt_size(loaded), size=fmt_size(size), speed=fmt_size(speed))
            if speed > 0 and loaded < size:
                detail += " · " + tr("dock.left", time=format_duration(max(1, int((size - loaded) / speed))))
            row_ratio = ratio
        elif loaded:
            ratio = 0.0
            detail = tr("dock.bytes", loaded=fmt_size(loaded), speed=fmt_size(speed))
            row_ratio = -1.0
        else:
            ratio = 0.0
            detail = tr("dock.ffmpeg")
            row_ratio = -1.0
        self._current = (job_id, key, position, total, item, row_ratio)
        job_ratio = ((position - 1) + ratio) / max(total, 1)
        self._job_progress[job_id] = job_ratio
        card = self.sidebar.card(job_id)
        if card is not None:
            card.set_progress(job_ratio)
        self.dock.set_progress(job_ratio)
        self.dock.set_text(detail=detail)
        if self.dock.thumb.pixmap() is None and item.thumb_url:
            self.dock.thumb.setPixmap(self._thumb_cache.pixmaps.get(item.thumb_url))
        if key == self._shown_key:
            self.model.set_progress(raw_id, row_ratio)

    def _on_idle(self, text: str, outcome: str) -> None:
        self._current = None
        self._job_progress.clear()
        if outcome == "paused":
            self.dock.show_paused(text)
        elif outcome in ("finished", "queue_finished"):
            if outcome == "queue_finished":
                title, detail = tr("dock.queue_finished"), self.controller.last_summary or text
            else:
                title, detail = tr("dock.done"), text
            self.dock.show_finished(title, detail, ok=True)
            self._notify(title, detail)
            if self._shutdown_after and not self.controller.pending_count():
                self._shut_down()
        elif outcome == "stopped":
            self.dock.show_finished(tr("dock.stopped"), text, ok=False)
        else:
            self.dock.dismiss()
        self._refresh_queue()
        self._update_bookmarks_view()

    # Previews

    def _deliver_thumb(self, tag, image) -> None:
        try:
            self._bridge.ready.emit(tag, image)
        except RuntimeError:
            pass  # the window is already closed

    def _request_cover(self, playlist: PlaylistInfo) -> None:
        self._request_cover_of(playlist.key, playlist.cover_url)

    def _request_cover_of(self, key: tuple, url: str) -> None:
        if url and key not in self._covers and (key, url) not in self._covers_asked:
            self._covers_asked.add((key, url))
            size = (round(COVER.width() * self._dpr), round(COVER.height() * self._dpr))
            self._thumbs.request(url, size, ("cover", key), "covers", radius=round(10 * self._dpr))

    def _on_thumb(self, tag: tuple, image) -> None:
        if tag[0] == "row":
            url = tag[1]
            self._thumb_cache.pixmaps[url] = images.to_pixmap(image, self._dpr)
            self.model.thumb_ready(url)
            if self._current is not None and self._current[4].thumb_url == url:
                self.dock.thumb.setPixmap(self._thumb_cache.pixmaps[url])
        elif tag[0] == "cover":
            key = tag[1]
            small_size = (round(CARD_THUMB.width() * self._dpr), round(CARD_THUMB.height() * self._dpr))
            small = image.resize(small_size, Image.LANCZOS)
            self._covers[key] = (images.to_pixmap(image, self._dpr), images.to_pixmap(small, self._dpr), images.backdrop(image))
            if self._bookmarks_view is not None:
                self._bookmarks_view.set_cover(self._bookmark_card_id(key), self._covers[key][1])
            if any(mark.key == key for mark in self._recent):
                self._update_recent()
            self._refresh_queue()

    # Context menus, folders, settings

    def _row_menu(self, pos: QPoint) -> None:
        index = self.list.indexAt(pos)
        if not index.isValid():
            return
        item: VideoItem = index.data(ITEM_ROLE)
        url = video_url(item)
        menu = polished(RoundMenu(parent=self))
        menu.addAction(Action(FluentIcon.GLOBE, tr("menu.browser"), triggered=lambda: QDesktopServices.openUrl(QUrl(url))))
        menu.addAction(Action(FluentIcon.COPY, tr("menu.copy_link"), triggered=lambda: QApplication.clipboard().setText(url)))
        ticked = [video_url(video) for _index, video in self.model.checked_items()]
        if ticked:
            menu.addAction(
                Action(
                    FluentIcon.COPY,
                    tr("menu.copy_ticked", count=len(ticked)),
                    triggered=lambda: QApplication.clipboard().setText("\n".join(ticked)),
                )
            )
        path = self._saved_file(item, index.data(INDEX_ROLE))
        if path is None or not path.exists():
            path = self.model.elsewhere.get(item.raw_id)
        if path is not None and path.exists():
            menu.addAction(Action(FluentIcon.FOLDER, tr("menu.reveal"), triggered=lambda: self._reveal(path)))
        place = index.data(INDEX_ROLE)
        menu.addAction(Action(FluentIcon.PHOTO, tr("menu.save_cover"), triggered=lambda: self._save_video_covers([(place, item)])))
        if len(ticked) > 1:
            menu.addAction(
                Action(
                    FluentIcon.PHOTO,
                    tr("menu.save_covers_ticked", count=len(ticked)),
                    triggered=lambda: self._save_video_covers(self.model.checked_items()),
                )
            )
        menu.addSeparator()
        if self._collection_shown():
            menu.addAction(Action(FluentIcon.REMOVE_FROM, tr("menu.uncollect"), triggered=lambda: self._uncollect([item.raw_id])))
        else:
            menu.addAction(Action(FluentIcon.ADD_TO, tr("menu.collect"), triggered=lambda: self._collect([item])))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _saved_file(self, item: VideoItem, index: int) -> Path | None:
        playlist = self.model.playlist
        if playlist is None:
            return None
        job = self.controller.job(self._view_job_id)
        width = name_width(len(playlist.videos))
        if job is not None and job.target_dir:
            folder, tagged = job.target_dir, job.tagged
        else:
            folder = self._target_dir(playlist)
            tagged = duplicate_ids(enumerate(playlist.videos, start=1), width, playlist.naming)
        return output_path(folder, item, index, width, playlist.naming, tagged)

    def _save_list_cover(self) -> None:
        """The cover of the open playlist, as «Title.jpg» beside its videos; of one open video,
        as the video's own file name."""
        playlist = self.model.playlist
        if playlist is None or playlist.kind not in COVER_KINDS or not self._can_save_here():
            return
        if playlist.single:
            self._save_video_covers([(1, playlist.videos[0])])
            return
        job = self.controller.job(self._view_job_id)
        folder = job.target_dir if job is not None and job.target_dir else self._target_dir(playlist)
        name = safe_filename(playlist.title) or "playlist"
        self.controller.save_covers([], album=(playlist, folder / f"{name}.jpg"))

    def _save_video_covers(self, pairs) -> None:
        """Previews of videos, given as (list index, video): each beside its video file and
        with its name, «001 - Title.jpg» for «001 - Title.mp4»."""
        if not self._can_save_here():
            return
        videos = [(item, path) for index, item in pairs if (path := self._saved_file(item, index)) is not None]
        if videos:
            self.controller.save_covers(videos)

    def _can_save_here(self) -> bool:
        if self.controller.job(self._view_job_id) is None and not self._folder.strip():
            self._toast("error", tr("toast.no_folder_title"), tr("toast.no_folder"))
            return False
        return True

    def _on_covers_saved(self, paths: list[Path], failed: int) -> None:
        if failed:
            self._toast("error", tr("toast.cover_failed"), tr("toast.cover_failed_text", count=failed))
        elif len(paths) == 1:
            self._toast("success", tr("toast.cover_saved"), short_path(str(paths[0]), 3))
        elif paths:
            self._toast("success", tr("toast.covers_saved", count=number(len(paths))), short_path(str(paths[0].parent), 3))

    def _reveal(self, path: Path) -> None:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            self._open_folder(path.parent)

    def _open_job_folder(self) -> None:
        job = self.controller.job(self._view_job_id)
        if job is not None:
            self._open_folder(job.target_dir)

    def _open_folder(self, folder) -> None:
        if not folder:
            return
        path = Path(folder)
        while not path.exists() and path.parent != path:
            path = path.parent
        if path != Path(folder):
            self._toast("info", tr("toast.no_folder_yet_title"), tr("toast.no_folder_yet"))
        if sys.platform == "win32":
            os.startfile(str(path))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, tr("dialog.folder"), self._folder or str(Path.home()))
        if chosen:
            self._folder = str(Path(chosen))
            self.hero.set_folder(self._folder)
            self._remember()
            self._recheck_saved()
            self._update_header()

    def _on_quality(self, quality: str) -> None:
        if not self._syncing:
            self._quality = quality
            self._remember()

    def _on_subfolder(self, checked: bool) -> None:
        if not self._syncing:
            self._subfolder = checked
            self._remember()
            self._recheck_saved()
            self._update_header()

    def _settings_menu(self) -> None:
        # One level with self-explaining items: no submenus that pop out past the window edge.
        menu = polished(CheckableMenu(parent=self, indicatorType=MenuIndicatorType.RADIO))
        self._add_choices(menu, {mode: tr(f"theme.{mode}") for mode in THEME_MODES}, self._theme_mode, self._set_theme)
        menu.addSeparator()
        self._add_choices(menu, i18n.LANGUAGES, i18n.language(), self.set_language)
        menu.addSeparator()
        check = Action(tr("settings.check_on_start"), checkable=True)
        check.setChecked(bool(self._settings.get("check_bookmarks")))
        check.triggered.connect(self._set_check_on_start)
        menu.addAction(check)
        if sys.platform == "win32":
            shutdown = Action(tr("settings.shutdown_after"), checkable=True)
            shutdown.setChecked(self._shutdown_after)
            shutdown.triggered.connect(self._set_shutdown_after)
            menu.addAction(shutdown)
        self._popup_above(menu, self.sidebar.settings_button, align_right=True)

    def _set_check_on_start(self, checked: bool) -> None:
        """Only a check and badges at start; nothing downloads by itself."""
        self._settings["check_bookmarks"] = bool(checked)
        self._remember()

    def _set_shutdown_after(self, checked: bool) -> None:
        self._shutdown_after = bool(checked)
        self.controller.note(tr("log.shutdown_on" if checked else "log.shutdown_off"))

    def _shut_down(self) -> None:
        """Ask Windows to shut down in SHUTDOWN_DELAY seconds, with a button that cancels it."""
        self._shutdown_after = False
        try:
            subprocess.run(
                ["shutdown", "/s", "/t", str(SHUTDOWN_DELAY)],
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            self.controller.note(tr("log.shutdown_failed", error=exc), "error")
            return
        text = tr("shutdown.text", seconds=SHUTDOWN_DELAY)
        self.controller.note(text, "warn")
        self._notify(tr("shutdown.title"), text)
        bar = InfoBar(
            InfoBarIcon.WARNING,
            tr("shutdown.title"),
            text,
            isClosable=False,
            duration=-1,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self.panel,
        )
        button = PushButton(tr("shutdown.cancel"), bar)

        def cancel():
            bar.close()
            subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.controller.note(tr("log.shutdown_cancelled"))

        button.clicked.connect(cancel)
        bar.addWidget(button)
        bar.show()
        self._bring_to_front()

    def _add_choices(self, menu: CheckableMenu, choices: dict, current: str, choose) -> None:
        group = QActionGroup(menu)
        group.setExclusive(True)
        for value, label in choices.items():
            action = Action(label, checkable=True)
            action.setChecked(value == current)
            action.triggered.connect(lambda _checked=False, value=value: choose(value))
            group.addAction(action)
            menu.addAction(action)

    def _popup_above(self, menu: RoundMenu, anchor: QWidget, *, align_right: bool = False) -> None:
        """Open a menu upwards from a button at the bottom of the window, lined up with its edge."""
        pos = anchor.mapToGlobal(QPoint(0, 0))
        menu.view.adjustSize(pos, MenuAnimationType.PULL_UP)
        menu.adjustSize()
        if align_right:
            pos.setX(pos.x() + anchor.width() - menu.view.width())
        menu.exec(pos, aniType=MenuAnimationType.PULL_UP)

    def _set_theme(self, mode: str) -> None:
        self._theme_mode = mode
        apply_theme(mode)
        self._remember()
        self._on_theme_changed()

    def _on_theme_changed(self) -> None:
        self.panel.refresh()
        self.hero.refresh_theme()
        self._paint_bookmarks_icon()
        self.log_drawer.refresh_theme()
        for widget in self.findChildren(QWidget):
            widget.update()

    def _toggle_log(self) -> None:
        if self.log_drawer.is_open():
            self.log_drawer.close_drawer()
            return
        self._errors_unseen = 0
        self.sidebar.set_errors(0)
        self.log_drawer.open_in(self._drawer_area())

    def _drawer_area(self) -> QRect:
        top = self.titleBar.height()
        return QRect(0, top, self.width(), self.height() - top - self.dock.height())

    def _toast(self, level: str, title: str, text: str, duration: int = 3500) -> None:
        method = {"success": InfoBar.success, "warning": InfoBar.warning, "error": InfoBar.error}.get(level, InfoBar.info)
        method(
            title,
            text,
            orient=Qt.Orientation.Vertical if len(text) > 70 or "\n" in text else Qt.Orientation.Horizontal,
            isClosable=True,
            duration=duration,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self.panel,
        )

    def _notify(self, title: str, text: str) -> None:
        if self.isActiveWindow() or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is None:
            self._tray = QSystemTrayIcon(self.windowIcon(), self)
            self._tray.messageClicked.connect(self._bring_to_front)
            self._tray.activated.connect(lambda _reason: self._bring_to_front())
        self._tray.setToolTip(tr("app.title"))
        self._tray.show()
        self._tray.showMessage(title, text, self.windowIcon(), 8000)
        QTimer.singleShot(15000, self._tray.hide)

    def _bring_to_front(self) -> None:
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        self.activateWindow()

    # Window events

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(150, self._offer_clipboard)
            # Files may have been moved or deleted meanwhile.
            QTimer.singleShot(0, self._recheck_saved)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "bookmarks_button"):
            self._fit_link_row()
        if hasattr(self, "log_drawer"):
            self.log_drawer.place(self._drawer_area())

    def dragEnterEvent(self, event) -> None:
        data = event.mimeData()
        if data.hasUrls() or data.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        data = event.mimeData()
        urls = data.urls() if data.hasUrls() else []
        files = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
        if files:
            # A .txt of links opens as if they were pasted: several videos make a collection.
            texts = []
            for path in files:
                if path.suffix.lower() != ".txt":
                    continue
                try:
                    texts.append(read_links_file(path))
                except OSError as exc:
                    self._toast("error", tr("toast.file_unread_title"), str(exc))
                    return
            text = "\n".join(texts)
        elif urls:
            text = "\n".join(url.toString() for url in urls)
        else:
            text = data.text()
        if text.strip():
            self.link.setText(" ".join(text.split()))
            self._open_link()
            event.acceptProposedAction()

    # Bookmarks

    def _bookmark(self, key: tuple | None) -> bookmarks.Bookmark | None:
        return next((mark for mark in self._bookmarks if mark.key == key), None)

    @staticmethod
    def _bookmark_card_id(key: tuple) -> str:
        return "bookmark:" + ":".join(str(part) for part in key)

    def _fit_link_row(self) -> None:
        """In a narrow window «Bookmarks» keeps only its star, so the link field stays wide enough."""
        narrow = self.panel.width() < 860
        self.bookmarks_button.setVisible(not narrow)
        self.bookmarks_star.setVisible(narrow)

    def _paint_bookmarks_icon(self) -> None:
        icon = images.star_icon(None, color("text"))
        self.bookmarks_button.setIcon(icon)
        self.bookmarks_star.setIcon(icon)

    def _bookmarks_anchor(self) -> QWidget:
        return self.bookmarks_star if self.bookmarks_star.isVisible() else self.bookmarks_button

    def _toggle_bookmark(self) -> None:
        playlist = self.model.playlist
        if playlist is None or self.pages.currentWidget() is not self.playlist_page:
            return
        mark = self._bookmark(playlist.key)
        if mark is not None:
            self._bookmarks.remove(mark)
            self._toast("info", tr("toast.bookmark_removed_title"), tr("toast.bookmark_removed", title=mark.title))
        else:
            mark = bookmarks.for_playlist(playlist, self.controller.sources.get(playlist.key))
            if mark is None:
                return
            self._bookmarks.insert(0, bookmarks.started(mark, playlist))
            self._toast("success", tr("toast.bookmark_added_title"), tr("toast.bookmark_added", title=mark.title))
        self._remember()
        self._update_header()

    def _refresh_bookmark(self, playlist: PlaylistInfo, announce: bool) -> None:
        """Keep a bookmark's name, cover and size as VK has them; say when videos were added."""
        mark = self._bookmark(playlist.key)
        if mark is None:
            return
        # Opened, it counts as checked: its badge now says how many of its videos are new.
        self._bookmark_checked[playlist.key] = ""
        fresh = bookmarks.refreshed(mark, playlist)
        if fresh != mark:
            self._bookmarks[self._bookmarks.index(mark)] = fresh
            self._remember()
        if not announce:
            return
        if fresh.kind == "owner":
            # The same videos as its badge: added after the bookmark's boundary, not saved yet.
            new = len(self._bookmark_new(playlist.key))
            if new:
                text = tr("toast.bookmark_unsaved", title=fresh.title, count=count(new, "video"))
                self._toast("info", tr("toast.bookmark_new_title"), text, duration=6000)
        elif mark.count and fresh.count > mark.count:
            added = count(fresh.count - mark.count, "video")
            self._toast("info", tr("toast.bookmark_new_title"), tr("toast.bookmark_new", title=fresh.title, count=added), duration=6000)

    def _bookmark_rows(self) -> list[CardData]:
        rows = []
        for mark in self._bookmarks:
            cover = self._covers.get(mark.key)
            if cover is None:
                self._request_cover_of(mark.key, mark.cover_url)
            amount = count(mark.count, "item") if mark.count else ""
            subtitle = " · ".join(part for part in (tr(f"bookmarks.kind.{mark.kind}"), amount) if part)
            status, role = self._bookmark_status(mark.key)
            rows.append(
                CardData(self._bookmark_card_id(mark.key), mark.title, subtitle, status, role, None, cover[1] if cover else None, tr("bookmarks.remove"))
            )
        return rows

    def _bookmark_status(self, key: tuple) -> tuple[str, str]:
        """The badge of a bookmark: how many new videos it has, once it was read in this run."""
        if key not in self._bookmark_checked:
            return "", "text2"
        if self._bookmark_checked[key]:
            return tr("bookmarks.check_failed"), "error"
        new = len(self._bookmark_new(key))
        return (tr("bookmarks.new", count=number(new)), "accent") if new else (tr("bookmarks.no_new"), "text3")

    def _bookmark_new(self, key: tuple) -> list[tuple[int, VideoItem]]:
        """(index, video) of a bookmark read in this run that are new for it (see bookmarks),
        not saved yet and not waiting in the queue. Videos saved meanwhile drop out."""
        mark = self._bookmark(key)
        if mark is None or self._bookmark_checked.get(key, "error"):
            return []
        playlist = self.controller.playlists.get(key)
        if playlist is None:
            return []
        queued = {
            item.raw_id
            for job in self.controller.jobs()
            if job.playlist_key == key and job.status not in FINISHED
            for _index, item in job.items
        }
        cached = self._unsaved.get(key)
        if cached is None or cached[0] is not playlist:
            cached = (playlist, self.controller.new_videos(playlist, Path(self._folder), self._subfolder))
            self._unsaved[key] = cached
        return [pair for pair in cached[1] if pair[1].raw_id not in queued and bookmarks.is_new(mark, pair[1])]

    def _update_bookmarks_view(self) -> None:
        """Badges in the open bookmarks list, and the total of new videos on the «Bookmarks» button."""
        total = sum(len(self._bookmark_new(mark.key)) for mark in self._bookmarks)
        heading = tr("bookmarks.heading_new", count=number(total)) if total else tr("bookmarks.heading")
        self.bookmarks_button.setText(heading)
        if self._bookmarks_view is not None:
            self._bookmarks_view.update_rows(self._bookmark_rows())
            self._bookmarks_view.set_state(self.controller.checking, total)

    def _show_bookmarks(self) -> None:
        view = BookmarksView(self._bookmark_rows())
        view.opened.connect(self._open_bookmark)
        view.removed.connect(self._remove_bookmark)
        view.menuRequested.connect(self._bookmark_menu)
        view.checkAll.connect(self._check_bookmarks)
        view.downloadAll.connect(lambda: self._queue_new([mark.key for mark in self._bookmarks]))
        flyout = Flyout.make(view, self._bookmarks_anchor(), self, aniType=FlyoutAnimationType.DROP_DOWN)
        self._bookmarks_view, self._bookmarks_flyout = view, flyout
        flyout.closed.connect(self._forget_bookmarks_view)
        self._update_bookmarks_view()

    def _bookmark_menu(self, card_id: str, pos: QPoint) -> None:
        mark = self._find_bookmark(card_id)
        if mark is None:
            return
        menu = polished(RoundMenu(parent=self))
        menu.addAction(Action(FluentIcon.VIEW, tr("bookmarks.open"), triggered=lambda: self._open_bookmark(card_id)))
        new = len(self._bookmark_new(mark.key))
        download = Action(FluentIcon.DOWNLOAD, tr("bookmarks.download_new", count=new), triggered=lambda: self._queue_new([mark.key]))
        download.setEnabled(new > 0)
        menu.addAction(download)
        if mark.kind == "owner":
            seen = Action(FluentIcon.ACCEPT, tr("bookmarks.mark_seen"), triggered=lambda: self._mark_seen(mark.key))
            seen.setEnabled(new > 0)
            menu.addAction(seen)
        menu.addSeparator()
        menu.addAction(Action(FluentIcon.DELETE, tr("bookmarks.remove"), triggered=lambda: self._drop_bookmark_card(card_id)))
        menu.exec(pos)

    def _mark_seen(self, key: tuple) -> None:
        """Move an owner page's boundary past what it has now: nothing of it is new until more comes."""
        mark = self._bookmark(key)
        playlist = self.controller.playlists.get(key)
        if mark is None or playlist is None:
            return
        self._bookmarks[self._bookmarks.index(mark)] = bookmarks.started(mark, playlist)
        self._remember()
        self._update_bookmarks_view()

    def _drop_bookmark_card(self, card_id: str) -> None:
        if self._bookmarks_view is not None and card_id in self._bookmarks_view.card_ids():
            self._bookmarks_view._remove(card_id)  # also removes the bookmark, through its signal
        else:
            self._remove_bookmark(card_id)

    def _check_bookmarks(self) -> None:
        """Read every bookmark from VK in the background and count its new videos."""
        if not self._bookmarks or not self.controller.check_lists([mark.target for mark in self._bookmarks]):
            return
        self._update_bookmarks_view()

    def _on_checked(self, key: tuple, playlist: PlaylistInfo | None, error: str) -> None:
        mark = self._bookmark(key)
        if playlist is None:
            self._bookmark_checked[key] = error or tr("bookmarks.check_failed")
            if mark is not None:
                self.controller.note(tr("log.check_failed", title=mark.title, error=error), "warn")
        else:
            self._bookmark_checked[key] = ""
            self._request_cover(playlist)
            if mark is not None:
                self._refresh_bookmark(playlist, announce=False)
            if key == self._shown_key and not self._view_job_id and self.pages.currentWidget() is self.playlist_page:
                # The list on screen was read just now: show it as VK has it, ticks kept.
                self._picks[key] = set(self.model.checked)
                self._display(key, "", force=True, keep_place=True)
        self._update_bookmarks_view()

    def _on_check_done(self, read: int, failed: int) -> None:
        total = sum(len(self._bookmark_new(mark.key)) for mark in self._bookmarks)
        text = tr("toast.check_done", count=videos_word(total)) if total else tr("toast.check_nothing")
        if failed:
            text += "\n" + tr("toast.check_failed", count=number(failed))
        self._toast("warning" if failed else "info", tr("toast.check_done_title"), text, duration=6000)
        self._update_bookmarks_view()

    def _queue_new(self, keys: list[tuple]) -> None:
        """Queue the new videos of these bookmarks, one job each, with the folder and quality set now."""
        if not self._folder.strip():
            self._toast("error", tr("toast.no_folder_title"), tr("toast.no_folder"))
            return
        jobs = []
        for key in keys:
            items = self._bookmark_new(key)
            playlist = self.controller.playlists.get(key)
            if items and playlist is not None:
                jobs.append(self.controller.make_job(playlist, items, Path(self._folder), self._quality, self._subfolder))
        if not jobs:
            self._toast("info", tr("toast.nothing_new_title"), tr("toast.nothing_new"))
            return
        self._remember()
        for job in jobs:
            self.controller.enqueue(job, start=False)
            if job.playlist_key == self._shown_key and not self._view_job_id:
                self._display(job.playlist_key, job.job_id, force=True)
        videos = sum(job.video_count for job in jobs)
        self._toast("success", tr("toast.queued"), tr("toast.new_queued", count=videos_word(videos), lists=number(len(jobs))))
        self._refresh_queue()
        self._select_view_card()
        self._update_bookmarks_view()

    def _forget_bookmarks_view(self) -> None:
        self._bookmarks_view = None
        self._bookmarks_flyout = None

    def _find_bookmark(self, card_id: str) -> bookmarks.Bookmark | None:
        return next((mark for mark in self._bookmarks if self._bookmark_card_id(mark.key) == card_id), None)

    def _open_bookmark(self, card_id: str) -> None:
        mark = self._find_bookmark(card_id)
        if mark is None or self.controller.loading:
            return
        if self._bookmarks_flyout is not None:
            self._bookmarks_flyout.close()
        # Always read from VK again: that is how new videos show up.
        self.controller.open_target(mark.target)
        self._show_link_error("")
        self._show_skeleton()
        self._update_controls()

    def _remove_bookmark(self, card_id: str) -> None:
        mark = self._find_bookmark(card_id)
        if mark is None:
            return
        self._bookmarks.remove(mark)
        self._bookmark_checked.pop(mark.key, None)
        self._remember()
        self._update_header()
        self._update_bookmarks_view()

    # Recently opened lists

    @staticmethod
    def _recent_card_id(key: tuple) -> str:
        return "recent:" + ":".join(str(part) for part in key)

    def _find_recent(self, card_id: str) -> bookmarks.Bookmark | None:
        return next((mark for mark in self._recent if self._recent_card_id(mark.key) == card_id), None)

    def _note_recent(self, playlist: PlaylistInfo) -> None:
        recent = bookmarks.remember(self._recent, playlist, self.controller.sources.get(playlist.key))
        if recent != self._recent:
            self._recent = recent
            self._remember()
            self._update_recent()

    def _recent_subtitle(self, mark: bookmarks.Bookmark) -> str:
        amount = count(mark.count, "item") if mark.count else ""
        return " · ".join(part for part in (tr(f"bookmarks.kind.{mark.kind}"), amount) if part)

    def _update_recent(self) -> None:
        """The latest lists as cards on the empty page."""
        rows = []
        for mark in self._recent[: self.empty.RECENT_SHOWN]:
            cover = self._covers.get(mark.key)
            if cover is None:
                self._request_cover_of(mark.key, mark.cover_url)
            rows.append(
                CardData(
                    self._recent_card_id(mark.key),
                    mark.title,
                    self._recent_subtitle(mark),
                    "",
                    "text2",
                    None,
                    cover[1] if cover else None,
                    tr("recent.forget"),
                )
            )
        self.empty.set_recent(rows)

    def _recent_menu(self) -> None:
        """All recent lists under the link field, newest first."""
        menu = polished(RoundMenu(parent=self))
        if not self._recent:
            empty = Action(FluentIcon.HISTORY, tr("recent.empty"))
            empty.setEnabled(False)
            menu.addAction(empty)
        for mark in self._recent:
            card_id = self._recent_card_id(mark.key)
            title = mark.title if len(mark.title) <= 60 else mark.title[:59] + "…"
            icon = FluentIcon.PEOPLE if mark.kind == "owner" else FluentIcon.VIDEO
            action = Action(icon, f"{title}  ·  {self._recent_subtitle(mark)}", triggered=lambda _=False, card_id=card_id: self._open_recent(card_id))
            action.setEnabled(not self.controller.loading)
            menu.addAction(action)
        if self._recent:
            menu.addSeparator()
            menu.addAction(Action(FluentIcon.BROOM, tr("recent.clear"), triggered=self._clear_recent))
        menu.exec(self.link.mapToGlobal(QPoint(0, self.link.height() + 4)))

    def _open_recent(self, card_id: str) -> None:
        mark = self._find_recent(card_id)
        if mark is None or self.controller.loading:
            return
        # Read from VK again, as a bookmark is: the list may have grown since.
        self.controller.open_target(mark.target)
        self._show_link_error("")
        self._show_skeleton()
        self._update_controls()

    def _forget_recent(self, card_id: str) -> None:
        mark = self._find_recent(card_id)
        if mark is None:
            return
        self._recent.remove(mark)
        self._remember()
        self._update_recent()

    def _clear_recent(self) -> None:
        self._recent = []
        self._remember()
        self._update_recent()

    # Keeping the queue between runs

    def _restore_session(self) -> None:
        if self._session_file is None:
            return
        try:
            state = session.load(self._session_file)
        except ValueError:
            self.controller.note(tr("log.restore_failed"), "warn")
            return
        if state is None:
            return
        pending = self.controller.restore(state)
        playlists = self.controller.playlists
        if state.draft in playlists:
            self._draft_key = state.draft
        if state.collection in playlists and playlists[state.collection].kind == "collection":
            self._collection_key = state.collection
        self._picks.update({key: ids for key, ids in state.picks.items() if key in playlists})
        for playlist in state.playlists.values():
            self._request_cover(playlist)
        if pending:
            self.dock.show_paused(tr("idle.paused", left=playlists_word(pending)))
            self.dock.set_text(title=tr("dock.restored"))

    def _schedule_save(self) -> None:
        if self._session_file is not None and not self._save_timer.isActive():
            self._save_timer.start()

    def _save_session(self) -> None:
        if self._session_file is None:
            return
        state = self.controller.session((self._draft_key, self._collection_key))
        state.draft = self._draft_key if self._draft_key in state.playlists else None
        state.collection = self._collection_key if self._collection_key in state.playlists else None
        picks = dict(self._picks)
        if self._shown_key is not None and not self._view_job_id and self.model.playlist is not None:
            picks[self._shown_key] = set(self.model.checked)
        state.picks = {key: picks[key] for key in (state.draft, state.collection) if key is not None and key in picks}
        text = session.dump(state)
        if text != self._session_text and session.save(self._session_file, text):
            self._session_text = text

    def _remember(self) -> None:
        geometry = self.normalGeometry()
        data = dict(self._settings)
        data.update(
            folder=self._folder.strip(),
            quality=self._quality,
            subfolder=self._subfolder,
            theme=self._theme_mode,
            language=i18n.language(),
            maximized=self.isMaximized(),
            bookmarks=bookmarks.to_data(self._bookmarks),
            recent=bookmarks.to_data(self._recent),
        )
        if geometry.isValid() and geometry.width() > 0:
            data["geometry"] = [geometry.x(), geometry.y(), geometry.width(), geometry.height()]
        self._settings = data
        save_settings(data)

    def _confirm_close(self) -> bool:
        text = tr("close.text_kept") if self._session_file is not None else tr("close.text")
        return self._ask(tr("close.title"), text, tr("close.yes"))

    def _ask(self, title: str, text: str, yes: str, no: str = "") -> bool:
        box = MessageBox(title, text, self)
        box.yesButton.setText(yes)
        box.cancelButton.setText(no or tr("close.cancel"))
        return bool(box.exec())

    def closeEvent(self, event) -> None:
        if self.controller.has_unfinished() and not self._confirm_close():
            event.ignore()
            return
        self.controller.close()
        self._save_timer.stop()
        self._save_session()
        self._thumbs.close()
        self._timer.stop()
        if self._theme_listener is not None and self._theme_listener.isRunning():
            self._theme_listener.terminate()
            self._theme_listener.wait(500)
        if self._tray is not None:
            self._tray.hide()
        self._remember()
        super().closeEvent(event)

# PyInstaller spec for VKDW.exe: a folder build that needs no Python and shows no console.
# Run build.bat; the result is dist\VKDW, with VKDW.exe and its _internal folder.

import importlib.metadata
import io
import os
import re
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.win32 import versioninfo as vi

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))

from vkdw import __version__  # noqa: E402


def make_icon(dest: Path) -> Path:
    """The window's icon, drawn at each size Windows asks for, as one .ico."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QGuiApplication

    from vkdw.ui.images import app_icon

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 (pixmaps need it)
    icon = app_icon()
    images = []
    for side in (16, 24, 32, 48, 64, 128, 256):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(side, side).save(buffer, "PNG")
        images.append(Image.open(io.BytesIO(bytes(buffer.data()))))
    dest.parent.mkdir(parents=True, exist_ok=True)
    largest = images[-1]
    largest.save(dest, sizes=[image.size for image in images], append_images=images[:-1])
    return dest


def version_info() -> vi.VSVersionInfo:
    """File properties of VKDW.exe: the version from vkdw/__init__.py and the product name."""
    numbers = [int(part) for part in re.match(r"\d+(?:\.\d+)*", __version__).group().split(".")]
    numbers = tuple((numbers + [0, 0, 0, 0])[:4])
    strings = [
        vi.StringStruct("CompanyName", "VKDW"),
        vi.StringStruct("FileDescription", "VK Video Downloader"),
        vi.StringStruct("FileVersion", __version__),
        vi.StringStruct("InternalName", "VKDW"),
        vi.StringStruct("OriginalFilename", "VKDW.exe"),
        vi.StringStruct("ProductName", "VKDW"),
        vi.StringStruct("ProductVersion", __version__),
    ]
    return vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
            vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


# Parts of Qt the window never uses, about 57 MB: software OpenGL; Qt Quick and QML, which
# only the virtual keyboard plugin needs; PDF pictures; Qt's own networking and TLS (the app
# goes online through Python); Qt's translations (QFluentWidgets keeps its texts inside itself).
UNUSED = (
    "opengl32sw.dll",
    "qt6quick",
    "qt6qml",
    "qt6opengl",
    "qt6virtualkeyboard",
    "qt6pdf",
    "qt6network",
    "libssl-3-x64.dll",
    "libcrypto-3-x64.dll",
    "plugins/platforminputcontexts/",
    "plugins/imageformats/qpdf",
    "plugins/tls/",
    "plugins/networkinformation/",
    "pyside6/translations/",
)


def used(entries):
    return [entry for entry in entries if not any(part in entry[0].replace("\\", "/").lower() for part in UNUSED)]


# Libraries whose own license files go along; Python's covers what it ships (OpenSSL, libffi...).
LICENSED = ("PySide6-Fluent-Widgets", "PySideSix-Frameless-Window", "darkdetect", "pillow", "certifi", "pywin32")


def licenses() -> list:
    """License texts of everything the folder carries besides VKDW itself, for _internal/licenses."""
    entries = [
        ("licenses/Python/LICENSE.txt", str(Path(sys.base_prefix) / "LICENSE.txt"), "DATA"),
        # Qt, PySide6 and shiboken6 come under the LGPL; their wheels name it but leave its text out.
        ("licenses/Qt and PySide6/LGPL-3.0.txt", str(ROOT / "packaging" / "LGPL-3.0.txt"), "DATA"),
    ]
    for name in LICENSED:
        dist = importlib.metadata.distribution(name)
        for file in dist.files or ():
            parts = file.parts
            if not parts[0].endswith(".dist-info"):
                continue
            rest = parts[1:]
            if rest[0] == "licenses":
                rest = rest[1:]
            elif not any(word in file.name.lower() for word in ("licen", "copying")):
                continue
            entries.append(("/".join(("licenses", name) + rest), str(dist.locate_file(file)), "DATA"))
    return entries


a = Analysis(
    [str(ROOT / "vkdw" / "__main__.py")],
    pathex=[str(ROOT)],
    # Never imported by the app; tkinter is left over in old environments.
    excludes=["tkinter", "unittest", "pydoc", "PyInstaller", "PySide6.QtNetwork"],
    noarchive=False,
)
a.binaries = used(a.binaries)
a.datas = used(a.datas) + licenses()
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VKDW",
    icon=str(make_icon(Path(workpath) / "VKDW.ico")),
    version=version_info(),
    console=False,
    # UPX-packed files are flagged by antivirus programs far more often.
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="VKDW", upx=False)
# VKDW's own license next to the exe, where people look for it.
shutil.copyfile(ROOT / "LICENSE", Path(DISTPATH) / "VKDW" / "LICENSE.txt")
